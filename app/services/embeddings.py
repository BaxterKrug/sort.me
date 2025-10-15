from typing import Any
import numpy as np
import torch
import torchvision.transforms as T
from PIL import Image


class SimpleEmbedder:
    """Image embedder using a torchvision resnet18 backbone.

    Accepts numpy arrays (BGR from OpenCV) or image file paths.
    """

    def __init__(self, device: str = 'cpu'):
        self.device = torch.device(device)
        # use a small torchvision model; keep only features
        self.model = torch.hub.load('pytorch/vision:v0.14.0', 'resnet18', pretrained=True)
        self.model = torch.nn.Sequential(*list(self.model.children())[:-1])
        self.model.eval().to(self.device)
        self.transform = T.Compose([
            T.Resize((224, 224)),
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        ])

    def _pil_from_input(self, image: Any) -> Image.Image:
        # Accept either a file path or a numpy array (OpenCV BGR)
        if isinstance(image, str):
            img = Image.open(image).convert('RGB')
            return img
        arr = np.asarray(image)
        # If OpenCV BGR, convert to RGB
        if arr.ndim == 3 and arr.shape[2] == 3:
            # assume BGR -> convert
            arr = arr[..., ::-1]
        return Image.fromarray(arr.astype('uint8'))

    def embed(self, image: Any):
        img = self._pil_from_input(image)
        x = self.transform(img).unsqueeze(0).to(self.device)
        with torch.no_grad():
            feat = self.model(x).squeeze()
        return feat.cpu().numpy()
