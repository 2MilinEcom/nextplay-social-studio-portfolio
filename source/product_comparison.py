import torch
import open_clip
from PIL import Image
import sys
import os
import torch.nn.functional as F

def load_and_preprocess(image_path, preprocess, device):
    image = Image.open(image_path).convert("RGB")
    return preprocess(image).unsqueeze(0).to(device)

def compare_images(img1_path, img2_path):
    # Load model
    model, _, preprocess = open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device)

    # Load and preprocess images
    img1_tensor = load_and_preprocess(img1_path, preprocess, device)
    img2_tensor = load_and_preprocess(img2_path, preprocess, device)

    with torch.no_grad():
        img1_feat = model.encode_image(img1_tensor)
        img2_feat = model.encode_image(img2_tensor)

    # Normalize
    img1_feat = F.normalize(img1_feat, dim=-1)
    img2_feat = F.normalize(img2_feat, dim=-1)

    # Cosine similarity
    similarity = (img1_feat @ img2_feat.T).item()

    print(f"\nSimilarity score between the two images: {similarity:.4f}")
    if similarity > 0.9: 
        print("✅ Very likely the same product.")
    elif similarity > 0.75:
        print("🟡 Possibly the same product.")
    else:
        print("❌ Probably different products.")

if __name__ == "__main__":
    img1 = "/Users/essencia/Downloads/masked.jpg"
    img2 = "/Users/essencia/Downloads/headphones.jpg"

    if not os.path.exists(img1) or not os.path.exists(img2):
        print("One or both image paths are invalid.")
        sys.exit(1)

    print(compare_images(img1, img2))
