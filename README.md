# DocuMentor

A terminal UI pipeline that converts PDF files to Markdown using [DeepSeek-OCR](https://huggingface.co/deepseek-ai/DeepSeek-OCR), a vision-language model. Pages are rendered to PNG at 300 DPI via PyMuPDF, passed through the model one page at a time, and written to a single `.md` file per PDF.

## Requirements

- Linux (tested on MX Linux)
- Python 3.12
- NVIDIA GPU with CUDA 12.1 support (model runs on GPU via PyTorch)
- ~10 GB free VRAM (RTX 3090 or similar recommended)
- [`glow`](https://github.com/charmbracelet/glow) for in-terminal Markdown preview (optional): `sudo apt install glow`

## Clone the repo

```bash
git clone https://github.com/davidblackuk/documentor.git
cd documentor
```

## Create the virtual environment

```bash
python3.12 -m venv .
```

> If Python 3.12 is not your system default, use the full path, e.g. `/usr/local/bin/python3.12 -m venv .`

Activate it:

```bash
source bin/activate
```

## Install dependencies

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
pip install transformers accelerate pymupdf rich pillow pyfiglet
```

## Download the model weights

The model weights are not included in this repo. Download them from Hugging Face:

```bash
pip install huggingface_hub
huggingface-cli download deepseek-ai/DeepSeek-OCR --local-dir ~/Documents/AI/ComfyUI/models/deepseek-ocr/deepseek-ai_DeepSeek-OCR
```

The path above matches the hardcoded `DEEPSEEK_PATH` in `documenter.py`. If you want to store the weights elsewhere, update that constant near the top of the file.

## Configure paths

`documenter.py` contains a hardcoded path for the model weights. If you store them in a different location, update `DEEPSEEK_PATH` near the top of the file.

## Run

```bash
# Using the launcher script:
./start.sh

# Or directly, with the venv active:
source bin/activate
python documenter.py
```

Drop PDFs into the `input/` directory. The TUI will list them and let you scan, rescan, or preview individual page ranges.

Output lands in `output/<pdf-stem>/<pdf-stem>.md` alongside an `images/` folder containing any extracted figures.
