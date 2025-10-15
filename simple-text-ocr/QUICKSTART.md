# Quickstart — simple-text-ocr

1. Install Homebrew (macOS) if not already installed: https://brew.sh
2. Install Tesseract OCR:
   brew install tesseract

3. Create a virtual environment and install Python dependencies:
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt

4. Run the demo CLI:
   python -m src.cli --image data/sample_images/example.jpg
