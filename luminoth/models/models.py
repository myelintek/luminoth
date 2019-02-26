from luminoth.models.fasterrcnn import FasterRCNN
from luminoth.models.ssd import SSD
from luminoth.models.yolov3 import YoloV3

# TODO: More models :)
MODELS = {
    'fasterrcnn': FasterRCNN,
    'ssd': SSD,
    'yolov3': YoloV3
}


def get_model(model_type):
    model_type = model_type.lower()
    if model_type not in MODELS:
        raise ValueError('"{}" is not a valid model_type'.format(model_type))

    return MODELS[model_type]
