import os

import cv2
import numpy as np
from PIL import Image
import pytorch_grad_cam.utils.image
import torch
import torchvision
import tqdm

import utils


def draw_cam(image: np.ndarray, mask: np.ndarray, colormap=cv2.COLORMAP_JET) -> np.ndarray:
    assert np.min(image) >= 0 and np.max(image) <= 1, 'Input image should in the range [0, 1]'

    heatmap = cv2.applyColorMap(np.uint8(mask * 255), colormap)
    heatmap = cv2.cvtColor(heatmap, cv2.COLOR_BGR2RGB)
    heatmap = np.float32(heatmap) / 255
    cam = image + heatmap
    cam = np.float32(cam) / np.max(cam)
    return np.uint8(cam * 255)


if __name__ == '__main__':
    # Load cfg and create components builder
    cfg = utils.builder.load_cfg()
    builder = utils.builder.Builder(cfg)

    # 1. Dataset
    valset, _ = builder.build_dataset('val')

    # 2. Model
    model = builder.build_model(valset.num_classes, pretrained=True)
    model.eval()
    model_name = cfg['model']['name']
    amp_enabled = cfg['model']['amp_enabled']
    print(f'Activated model: {model_name}')

    # 이미지 불러오기
    image_number = input('Enter the image number of the dataset>>> ')
    if image_number == '':
        image_number = 3
    else:
        image_number = int(image_number)
    image, _ = valset[image_number]
    image.unsqueeze_(0)

    # Class activation map을 생성할 계층을 지정
    gradcam_layers = {
        'backbone': pytorch_grad_cam.GradCAM(model, target_layer=model.backbone, use_cuda=torch.cuda.is_available()),
        'aspp': pytorch_grad_cam.GradCAM(model, target_layer=model.aspp, use_cuda=torch.cuda.is_available()),
        'decoder': pytorch_grad_cam.GradCAM(model, target_layer=model.decoder, use_cuda=torch.cuda.is_available()),
    }

    # Class activation map 생성
    for layer, gradcam in tqdm.tqdm(gradcam_layers.items(), desc='Saving CAM'):
        result_dir = os.path.join('cam', model_name, layer)
        os.makedirs(result_dir, exist_ok=True)

        for target_category in tqdm.tqdm(range(valset.num_classes), desc='Classes', leave=False):
            cam_mask: np.ndarray = gradcam(image, target_category)[0, :]
            visualization = draw_cam(np.array(Image.open(valset.images[image_number]).convert('RGB')) / 255, cam_mask)
            torchvision.utils.save_image(
                torchvision.transforms.ToTensor()(visualization),
                os.path.join(result_dir, f'{target_category}_{valset.class_names[target_category]}.png')
            )