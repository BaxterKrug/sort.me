from typing import List
import torch
import torchvision.transforms as T
from PIL import Image


class SimpleEmbedder:
    """Simple image embedder using a small torchvision model (resnet18 backbone).

    This is a minimal example — for production you'd pick a better pre-trained
    model and possibly use faiss for fast retrieval.
    """

    def __init__(self, device: str = 'cpu'):
        self.device = torch.device(device)
        self.model = torch.hub.load('pytorch/vision:v0.14.0', 'resnet18', pretrained=True)
        self.model = torch.nn.Sequential(*list(self.model.children())[:-1])
        self.model.eval().to(self.device)
        self.transform = T.Compose([
            T.Resize((224, 224)),
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        ])

    def embed(self, image_path: str):
        img = Image.open(image_path).convert('RGB')
        x = self.transform(img).unsqueeze(0).to(self.device)
        with torch.no_grad():
            feat = self.model(x).squeeze()
        return feat.cpu().numpy()
