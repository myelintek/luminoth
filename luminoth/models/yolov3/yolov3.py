import sonnet as snt
import tensorflow as tf

slim = tf.contrib.slim

ANCHORS=[(10, 13), (16, 30), (33, 23), (30, 61), (62, 45), (59, 119), (116, 90), (156, 198), (373, 326)]
def _conv2d_fixed_padding(inputs, filters, kernel_size, strides=1):
    if strides > 1: inputs = _fixed_padding(inputs, kernel_size)
    inputs = slim.conv2d(inputs, filters, kernel_size, stride=strides,
                         padding=('SAME' if strides == 1 else 'VALID'))
    return inputs

def _fixed_padding(inputs, kernel_size, mode='CONSTANT', *args, **kwargs):
    """
    Pads the input along the spatial dimensions independently of input size.
    Args:
      inputs: A tensor of size [batch, channels, height_in, width_in] or
        [batch, height_in, width_in, channels] depending on data_format.
      kernel_size: The kernel to be used in the conv2d or max_pool2d operation.
                   Should be a positive integer.
      mode: The mode for tf.pad.
    Returns:
      A tensor with the same format as the input with the data either intact
      (if kernel_size == 1) or padded (if kernel_size > 1).
    """
    pad_total = kernel_size - 1
    pad_beg = pad_total // 2
    pad_end = pad_total - pad_beg

    padded_inputs = tf.pad(inputs, [[0, 0], [pad_beg, pad_end],
                                    [pad_beg, pad_end], [0, 0]], mode=mode)
    return padded_inputs

class darknet53(object):
    """network for performing feature extraction"""

    def __init__(self, inputs):
        self.outputs = self.forward(inputs)

    def _darknet53_block(self, inputs, filters):
        """
        implement residuals block in darknet53
        """
        shortcut = inputs
        inputs = _conv2d_fixed_padding(inputs, filters * 1, 1)
        inputs = _conv2d_fixed_padding(inputs, filters * 2, 3)

        inputs = inputs + shortcut
        return inputs

    def forward(self, inputs):

        inputs = _conv2d_fixed_padding(inputs, 32,  3, strides=1)
        inputs = _conv2d_fixed_padding(inputs, 64,  3, strides=2)
        inputs = self._darknet53_block(inputs, 32)
        inputs = _conv2d_fixed_padding(inputs, 128, 3, strides=2)

        for i in range(2):
            inputs = self._darknet53_block(inputs, 64)

        inputs = _conv2d_fixed_padding(inputs, 256, 3, strides=2)

        for i in range(8):
            inputs = self._darknet53_block(inputs, 128)

        route_1 = inputs
        inputs = _conv2d_fixed_padding(inputs, 512, 3, strides=2)

        for i in range(8):
            inputs = self._darknet53_block(inputs, 256)

        route_2 = inputs
        inputs = _conv2d_fixed_padding(inputs, 1024, 3, strides=2)

        for i in range(4):
            inputs = self._darknet53_block(inputs, 512)

        return route_1, route_2, inputs


class YoloV3(snt.AbstractModule):

    def __init__(self, config, debug=False, seed=None,
                 name='yolov3'):
        super(YoloV3, self).__init__(name=name)
        # self._ANCHORS = [[10 ,13], [16 , 30], [33 , 23],
                         # [30 ,61], [62 , 45], [59 ,119],
                         # [116,90], [156,198], [373,326]]
        self._ANCHORS = ANCHORS
        self._BATCH_NORM_DECAY = config.model.network.batch_norm_decay
        self._LEAKY_RELU = config.model.network.leaky_relu
        self._NUM_CLASSES = config.model.network.num_classes
        self.feature_maps = [] # [[None, 13, 13, 255], [None, 26, 26, 255], [None, 52, 52, 255]]
        self.image_shape = [config.dataset.image_preprocessing.fixed_height,
                            config.dataset.image_preprocessing.fixed_width]
        self._config=config

    def _yolo_block(self, inputs, filters):
        inputs = _conv2d_fixed_padding(inputs, filters * 1, 1)
        inputs = _conv2d_fixed_padding(inputs, filters * 2, 3)
        inputs = _conv2d_fixed_padding(inputs, filters * 1, 1)
        inputs = _conv2d_fixed_padding(inputs, filters * 2, 3)
        inputs = _conv2d_fixed_padding(inputs, filters * 1, 1)
        route = inputs
        inputs = _conv2d_fixed_padding(inputs, filters * 2, 3)
        return route, inputs

    def _detection_layer(self, inputs, anchors):
        num_anchors = len(anchors)
        feature_map = slim.conv2d(inputs, num_anchors * (5 + self._NUM_CLASSES), 1,
                                stride=1, normalizer_fn=None,
                                activation_fn=None,
                                biases_initializer=tf.zeros_initializer())
        return feature_map

    def _reorg_layer(self, feature_map, anchors):

        num_anchors = len(anchors) # num_anchors=3
        grid_size = feature_map.shape.as_list()[1:3]
        # the downscale image in height and weight
        stride = tf.cast(self.img_size // grid_size, tf.float32) # [h,w] -> [y,x]
        feature_map = tf.reshape(feature_map, [-1, grid_size[0], grid_size[1], num_anchors, 5 + self._NUM_CLASSES])

        box_centers, box_sizes, conf_logits, prob_logits = tf.split(
            feature_map, [2, 2, 1, self._NUM_CLASSES], axis=-1)

        box_centers = tf.nn.sigmoid(box_centers)

        grid_x = tf.range(grid_size[1], dtype=tf.int32)
        grid_y = tf.range(grid_size[0], dtype=tf.int32)

        a, b = tf.meshgrid(grid_x, grid_y)
        x_offset   = tf.reshape(a, (-1, 1))
        y_offset   = tf.reshape(b, (-1, 1))
        x_y_offset = tf.concat([x_offset, y_offset], axis=-1)
        x_y_offset = tf.reshape(x_y_offset, [grid_size[0], grid_size[1], 1, 2])
        x_y_offset = tf.cast(x_y_offset, tf.float32)

        box_centers = box_centers + x_y_offset
        box_centers = box_centers * stride[::-1]
        box_sizes = tf.exp(box_sizes) * anchors # anchors -> [w, h]
        boxes = tf.concat([box_centers, box_sizes], axis=-1)
        return x_y_offset, boxes, conf_logits, prob_logits

    @staticmethod
    def _upsample(inputs, out_shape):

        new_height, new_width = out_shape[1], out_shape[2]
        inputs = tf.image.resize_nearest_neighbor(inputs, (new_height, new_width))
        inputs = tf.identity(inputs, name='upsampled')

        return inputs

    # @staticmethod
    # def _upsample(inputs, out_shape):
        # """
        # replace resize_nearest_neighbor with conv2d_transpose To support TensorRT 5 optimization
        # """
        # new_height, new_width = out_shape[1], out_shape[2]
        # filters = 256 if (new_height == 26 and new_width==26) else 128
        # inputs = tf.layers.conv2d_transpose(inputs, filters, kernel_size=3, padding='same',
                                            # strides=(2,2), kernel_initializer=tf.ones_initializer())
        # return inputs

    def _build(self, inputs, y_true, is_training=False, reuse=False):
        """
        Creates YOLO v3 model.

        :param inputs: a 4-D tensor of size [batch_size, height, width, channels].
               Dimension batch_size may be undefined. The channel order is RGB.
        :param is_training: whether is training or not.
        :param reuse: whether or not the network and its variables should be reused.
        :return:
        """
        # it will be needed later on
        #self.img_size = tf.shape(inputs)[1:3]
        #print("inputs*******************************")
        #print(inputs)
        #print("y_true*******************************")
        y_true = tf.dtypes.cast(y_true, tf.float32)

        #print(y_true)
        self.img_size = tf.constant(list(self.image_shape))
        #print("img_size*****************************")
        #print(self.img_size)
        # [N H W C]
        self.image_shape.append(3)  # Add channels to shape
        inputs.set_shape(self.image_shape)
        # [HWC] to [N H W C]
        inputs = tf.expand_dims(inputs, 0, name='hardcode_batch_size_to_1')

        #print("inputs after************************")
        #print(inputs)
        # set batch norm params
        batch_norm_params = {
            'decay': self._BATCH_NORM_DECAY,
            'epsilon': 1e-05,
            'scale': True,
            'is_training': is_training,
            'fused': None,  # Use fused batch norm if possible.
        }

        # Set activation_fn and parameters for conv2d, batch_norm.
        with slim.arg_scope([slim.conv2d, slim.batch_norm],reuse=reuse):
            with slim.arg_scope([slim.conv2d], normalizer_fn=slim.batch_norm,
                                normalizer_params=batch_norm_params,
                                biases_initializer=None,
                                activation_fn=lambda x: tf.nn.leaky_relu(x, alpha=self._LEAKY_RELU)):
                with tf.variable_scope('darknet-53'):
                    route_1, route_2, inputs = darknet53(inputs).outputs

                with tf.variable_scope('yolo-v3'):
                    route, inputs = self._yolo_block(inputs, 512)
                    feature_map_1 = self._detection_layer(inputs, self._ANCHORS[6:9])
                    feature_map_1 = tf.identity(feature_map_1, name='feature_map_1')

                    inputs = _conv2d_fixed_padding(route, 256, 1)
                    upsample_size = route_2.get_shape().as_list()
                    inputs = self._upsample(inputs, upsample_size)
                    inputs = tf.concat([inputs, route_2], axis=3)

                    route, inputs = self._yolo_block(inputs, 256)
                    feature_map_2 = self._detection_layer(inputs, self._ANCHORS[3:6])
                    feature_map_2 = tf.identity(feature_map_2, name='feature_map_2')

                    inputs = _conv2d_fixed_padding(route, 128, 1)
                    upsample_size = route_1.get_shape().as_list()
                    inputs = self._upsample(inputs, upsample_size)
                    inputs = tf.concat([inputs, route_1], axis=3)

                    route, inputs = self._yolo_block(inputs, 128)
                    feature_map_3 = self._detection_layer(inputs, self._ANCHORS[0:3])
                    feature_map_3 = tf.identity(feature_map_3, name='feature_map_3')

            return {'feature_maps': [feature_map_1, feature_map_2, feature_map_3],
                    'y_true': y_true}

    def _reshape(self, x_y_offset, boxes, confs, probs):

        grid_size = x_y_offset.shape.as_list()[:2]
        boxes = tf.reshape(boxes, [-1, grid_size[0]*grid_size[1]*3, 4])
        confs = tf.reshape(confs, [-1, grid_size[0]*grid_size[1]*3, 1])
        probs = tf.reshape(probs, [-1, grid_size[0]*grid_size[1]*3, self._NUM_CLASSES])

        return boxes, confs, probs

    def predict(self, feature_maps):
        """
        Note: given by feature_maps, compute the receptive field
              and get boxes, confs and class_probs
        input_argument: feature_maps -> [None, 13, 13, 255],
                                        [None, 26, 26, 255],
                                        [None, 52, 52, 255],
        """
        feature_map_1, feature_map_2, feature_map_3 = feature_maps
        feature_map_anchors = [(feature_map_1, self._ANCHORS[6:9]),
                               (feature_map_2, self._ANCHORS[3:6]),
                               (feature_map_3, self._ANCHORS[0:3]),]

        results = [self._reorg_layer(feature_map, anchors) for (feature_map, anchors) in feature_map_anchors]
        boxes_list, confs_list, probs_list = [], [], []

        for result in results:
            boxes, conf_logits, prob_logits = self._reshape(*result)

            confs = tf.sigmoid(conf_logits)
            probs = tf.sigmoid(prob_logits)

            boxes_list.append(boxes)
            confs_list.append(confs)
            probs_list.append(probs)

        boxes = tf.concat(boxes_list, axis=1)
        confs = tf.concat(confs_list, axis=1)
        probs = tf.concat(probs_list, axis=1)

        center_x, center_y, width, height = tf.split(boxes, [1,1,1,1], axis=-1)
        x0 = center_x - width   / 2.
        y0 = center_y - height  / 2.
        x1 = center_x + width   / 2.
        y1 = center_y + height  / 2.

        boxes = tf.concat([x0, y0, x1, y1], axis=-1)
        return boxes, confs, probs

    def loss(self, inputs, ignore_thresh=0.5, max_box_per_image=8):
        """
        Note: compute the loss
        Arguments: y_pred, list -> [feature_map_1, feature_map_2, feature_map_3]
                                        the shape of [None, 13, 13, 3*85]. etc
        """
        
        pred_feature_map = inputs['feature_maps']
        y_true = inputs['y_true']
        loss_xy, loss_wh, loss_conf, loss_class = 0., 0., 0., 0.
        total_loss = 0.
        # total_loss, rec_50, rec_75,  avg_iou    = 0., 0., 0., 0.
        _ANCHORS = [self._ANCHORS[6:9], self._ANCHORS[3:6], self._ANCHORS[0:3]]

        for i in range(len(pred_feature_map)):
            result = self.loss_layer(pred_feature_map[i], y_true[i], _ANCHORS[i])
            loss_xy    += result[0]
            loss_wh    += result[1]
            loss_conf  += result[2]
            loss_class += result[3]

        total_loss = loss_xy + loss_wh + loss_conf + loss_class
        return total_loss


    def loss_layer(self, feature_map_i, y_true, anchors):
        # size in [h, w] format! don't get messed up!
        grid_size = tf.shape(feature_map_i)[1:3]
        grid_size_ = feature_map_i.shape.as_list()[1:3]

        y_true = tf.reshape(y_true, [-1, grid_size_[0], grid_size_[1], 3, 5+self._NUM_CLASSES])

        # the downscale ratio in height and weight
        ratio = tf.cast(self.img_size / grid_size, tf.float32)
        # N: batch_size
        N = tf.cast(tf.shape(feature_map_i)[0], tf.float32)

        x_y_offset, pred_boxes, pred_conf_logits, pred_prob_logits = self._reorg_layer(feature_map_i, anchors)
        # shape: take 416x416 input image and 13*13 feature_map for example:
        # [N, 13, 13, 3, 1]
        object_mask = y_true[..., 4:5]
        # shape: [N, 13, 13, 3, 4] & [N, 13, 13, 3] ==> [V, 4]
        # V: num of true gt box
        valid_true_boxes = tf.boolean_mask(y_true[..., 0:4], tf.cast(object_mask[..., 0], 'bool'))

        # shape: [V, 2]
        valid_true_box_xy = valid_true_boxes[:, 0:2]
        valid_true_box_wh = valid_true_boxes[:, 2:4]
        # shape: [N, 13, 13, 3, 2]
        pred_box_xy = pred_boxes[..., 0:2]
        pred_box_wh = pred_boxes[..., 2:4]

        # calc iou
        # shape: [N, 13, 13, 3, V]
        iou = self._broadcast_iou(valid_true_box_xy, valid_true_box_wh, pred_box_xy, pred_box_wh)

        # shape: [N, 13, 13, 3]
        best_iou = tf.reduce_max(iou, axis=-1)
        # get_ignore_mask
        ignore_mask = tf.cast(best_iou < 0.5, tf.float32)
        # shape: [N, 13, 13, 3, 1]
        ignore_mask = tf.expand_dims(ignore_mask, -1)
        # get xy coordinates in one cell from the feature_map
        # numerical range: 0 ~ 1
        # shape: [N, 13, 13, 3, 2]
        
        true_xy = y_true[..., 0:2] / ratio[::-1] - x_y_offset
        pred_xy = pred_box_xy      / ratio[::-1] - x_y_offset

        # get_tw_th, numerical range: 0 ~ 1
        # shape: [N, 13, 13, 3, 2]
        true_tw_th = y_true[..., 2:4] / anchors
        pred_tw_th = pred_box_wh      / anchors
        # for numerical stability
        true_tw_th = tf.where(condition=tf.equal(true_tw_th, 0),
                              x=tf.ones_like(true_tw_th), y=true_tw_th)
        pred_tw_th = tf.where(condition=tf.equal(pred_tw_th, 0),
                              x=tf.ones_like(pred_tw_th), y=pred_tw_th)

        true_tw_th = tf.log(tf.clip_by_value(true_tw_th, 1e-9, 1e9))
        pred_tw_th = tf.log(tf.clip_by_value(pred_tw_th, 1e-9, 1e9))

        # box size punishment:
        # box with smaller area has bigger weight. This is taken from the yolo darknet C source code.
        # shape: [N, 13, 13, 3, 1]
        box_loss_scale = 2. - (y_true[..., 2:3] / tf.cast(self.img_size[1], tf.float32)) * (y_true[..., 3:4] / tf.cast(self.img_size[0], tf.float32))

        # shape: [N, 13, 13, 3, 1]
        xy_loss = tf.reduce_sum(tf.square(true_xy    - pred_xy) * object_mask * box_loss_scale) / N
        wh_loss = tf.reduce_sum(tf.square(true_tw_th - pred_tw_th) * object_mask * box_loss_scale) / N

        # shape: [N, 13, 13, 3, 1]
        conf_pos_mask = object_mask
        conf_neg_mask = (1 - object_mask) * ignore_mask
        conf_loss_pos = conf_pos_mask * tf.nn.sigmoid_cross_entropy_with_logits(labels=object_mask, logits=pred_conf_logits)
        conf_loss_neg = conf_neg_mask * tf.nn.sigmoid_cross_entropy_with_logits(labels=object_mask, logits=pred_conf_logits)
        conf_loss = tf.reduce_sum(conf_loss_pos + conf_loss_neg) / N

        # shape: [N, 13, 13, 3, 1]
        class_loss = object_mask * tf.nn.sigmoid_cross_entropy_with_logits(labels=y_true[..., 5:], logits=pred_prob_logits)
        class_loss = tf.reduce_sum(class_loss) / N

        return xy_loss, wh_loss, conf_loss, class_loss

    def _broadcast_iou(self, true_box_xy, true_box_wh, pred_box_xy, pred_box_wh):
        '''
        maintain an efficient way to calculate the ios matrix between ground truth true boxes and the predicted boxes
        note: here we only care about the size match
        '''
        # shape:
        # true_box_??: [V, 2]
        # pred_box_??: [N, 13, 13, 3, 2]

        # shape: [N, 13, 13, 3, 1, 2]
        #print(true_box_xy.dtype)
        #print(tf.float32)
        #if true_box_xy.dtype != tf.float32:
        #    true_box_xy = tf.dtypes.cast(true_box_xy, dtype=tf.float32)
        #if true_box_wh.dtype != tf.float32:
        #    true_box_wh = tf.dtypes.cast(true_box_wh, dtype=tf.float32)

        pred_box_xy = tf.expand_dims(pred_box_xy, -2)
        pred_box_wh = tf.expand_dims(pred_box_wh, -2)

        # shape: [1, V, 2]
        true_box_xy = tf.expand_dims(true_box_xy, 0)
        true_box_wh = tf.expand_dims(true_box_wh, 0)

        #print(pred_box_xy)
        #print(pred_box_wh)
        #print(true_box_xy)
        #print(true_box_wh)
        # [N, 13, 13, 3, 1, 2] & [1, V, 2] ==> [N, 13, 13, 3, V, 2]
        intersect_mins = tf.maximum(pred_box_xy - pred_box_wh / 2.,
                                    true_box_xy - true_box_wh / 2.)
        intersect_maxs = tf.minimum(pred_box_xy + pred_box_wh / 2.,
                                    true_box_xy + true_box_wh / 2.)
        intersect_wh = tf.maximum(intersect_maxs - intersect_mins, 0.)

        # shape: [N, 13, 13, 3, V]
        intersect_area = intersect_wh[..., 0] * intersect_wh[..., 1]
        # shape: [N, 13, 13, 3, 1]
        pred_box_area  = pred_box_wh[..., 0]  * pred_box_wh[..., 1]
        # shape: [1, V]
        true_box_area  = true_box_wh[..., 0]  * true_box_wh[..., 1]
        # [N, 13, 13, 3, V]
        iou = intersect_area / (pred_box_area + true_box_area - intersect_area)

        return iou

    def get_trainable_vars(self):
        """Get trainable vars included in the module.
        """
        trainable_vars = snt.get_variables_in_module(self)
        if False: #self._config.model.base_network.trainable:  TODO
            pretrained_trainable_vars = (
                self.feature_extractor.get_trainable_vars()
            )
            tf.logging.info('Training {} vars from pretrained module.'.format(
                len(pretrained_trainable_vars)))
            trainable_vars += pretrained_trainable_vars
        else:
            tf.logging.info('Not training variables from pretrained module')

        return trainable_vars


    def get_base_network_checkpoint_vars(self):
        return None #TODO

    def get_checkpoint_file(self):
        return None #TODO
    
    def summary(self):
        """
        Generate merged summary of all the sub-summaries used inside the
        network.
        """
        #summaries = [
        #    tf.summary.merge_all()
        #]

        #return tf.summary.merge(summaries)
        return None #TODO

    def preprocess(self, image, gt_boxes):
        # resize_image_correct_bbox
        image, gt_boxes = self.resize_image_correct_bbox(image, gt_boxes, self.image_shape[0], self.image_shape[1])
        if self.debug: return image, gt_boxes

        y_true_13, y_true_26, y_true_52 = tf.py_func(self.preprocess_true_boxes, inp=[gt_boxes],
                            Tout = [tf.float32, tf.float32, tf.float32])
        # data augmentation
        # pass
        image = image / 255.

        return image, y_true_13, y_true_26, y_true_52

    def preprocess_true_boxes(self, gt_boxes):
        """
        Preprocess true boxes to training input format
        Parameters:
        -----------
        :param true_boxes: numpy.ndarray of shape [T, 4]
                            T: the number of boxes in each image.
                            4: coordinate => x_min, y_min, x_max, y_max
        :param true_labels: class id
        :param input_shape: the shape of input image to the yolov3 network, [416, 416]
        :param anchors: array, shape=[9,2], 9: the number of anchors, 2: width, height
        :param num_classes: integer, for coco dataset, it is 80
        Returns:
        ----------
        y_true: list(3 array), shape like yolo_outputs, [13, 13, 3, 85]
                            13:cell szie, 3:number of anchors
                            85: box_centers, box_sizes, confidence, probability
        """
        num_layers = len(self.anchors) // 3
        anchor_mask = [[6,7,8], [3,4,5], [0,1,2]] if num_layers==3 else [[3,4,5], [1,2,3]]
        grid_sizes = [[self.image_shape[0]//x, self.image_shape[1]//x] for x in (32, 16, 8)]

        box_centers = (gt_boxes[:, 0:2] + gt_boxes[:, 2:4]) / 2 # the center of box
        box_sizes =    gt_boxes[:, 2:4] - gt_boxes[:, 0:2] # the height and width of box

        gt_boxes[:, 0:2] = box_centers
        gt_boxes[:, 2:4] = box_sizes

        y_true_13 = np.zeros(shape=[grid_sizes[0][0], grid_sizes[0][1], 3, 5+self.num_classes], dtype=np.float32)
        y_true_26 = np.zeros(shape=[grid_sizes[1][0], grid_sizes[1][1], 3, 5+self.num_classes], dtype=np.float32)
        y_true_52 = np.zeros(shape=[grid_sizes[2][0], grid_sizes[2][1], 3, 5+self.num_classes], dtype=np.float32)

        y_true = [y_true_13, y_true_26, y_true_52]
        anchors_max =  self._ANCHORS / 2.
        anchors_min = -anchors_max
        valid_mask = box_sizes[:, 0] > 0

        # Discard zero rows.
        wh = box_sizes[valid_mask]
        # set the center of all boxes as the origin of their coordinates
        # and correct their coordinates
        wh = np.expand_dims(wh, -2)
        boxes_max = wh / 2.
        boxes_min = -boxes_max

        intersect_mins = np.maximum(boxes_min, anchors_min)
        intersect_maxs = np.minimum(boxes_max, anchors_max)
        intersect_wh   = np.maximum(intersect_maxs - intersect_mins, 0.)
        intersect_area = intersect_wh[..., 0] * intersect_wh[..., 1]
        box_area       = wh[..., 0] * wh[..., 1]

        anchor_area = self.anchors[:, 0] * self.anchors[:, 1]
        iou = intersect_area / (box_area + anchor_area - intersect_area)
        # Find best anchor for each true box
        best_anchor = np.argmax(iou, axis=-1)

        for t, n in enumerate(best_anchor):
            for l in range(num_layers):
                if n not in anchor_mask[l]: continue

                i = np.floor(gt_boxes[t,0]/self.image_w*grid_sizes[l][1]).astype('int32')
                j = np.floor(gt_boxes[t,1]/self.image_h*grid_sizes[l][0]).astype('int32')

                k = anchor_mask[l].index(n)
                c = gt_boxes[t, 4].astype('int32')

                y_true[l][j, i, k, 0:4] = gt_boxes[t, 0:4]
                y_true[l][j, i, k,   4] = 1.
                y_true[l][j, i, k, 5+c] = 1.

        return y_true_13, y_true_26, y_true_52

    def resize_image_correct_bbox(image, boxes, image_h, image_w):

        origin_image_size = tf.to_float(tf.shape(image)[0:2])
        image = tf.image.resize_images(image, size=[image_h, image_w])

        # correct bbox
        xx1 = boxes[:, 0] * image_w / origin_image_size[1]
        yy1 = boxes[:, 1] * image_h / origin_image_size[0]
        xx2 = boxes[:, 2] * image_w / origin_image_size[1]
        yy2 = boxes[:, 3] * image_h / origin_image_size[0]
        idx = boxes[:, 4]

        boxes = tf.stack([xx1, yy1, xx2, yy2, idx], axis=1)
        return image, boxes

