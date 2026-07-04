import os, sys, re, shutil, tempfile
import torch
import fitz  # pymupdf
from transformers import AutoModel, AutoTokenizer

MODEL_PATH = "/home/davidb/Documents/AI/ComfyUI/models/deepseek-ocr/deepseek-ai_DeepSeek-OCR"
INPUT_DIR  = sys.argv[1] if len(sys.argv) > 1 else "./input"
OUTPUT_DIR = sys.argv[2] if len(sys.argv) > 2 else "./output"

os.makedirs(OUTPUT_DIR, exist_ok=True)

pdf_files = sorted(f for f in os.listdir(INPUT_DIR) if f.lower().endswith(".pdf"))
if not pdf_files:
    print(f"No PDFs found in {INPUT_DIR}")
    sys.exit(0)

print(f"Found {len(pdf_files)} PDF(s). Loading model...")

tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, trust_remote_code=True)
model = AutoModel.from_pretrained(
    MODEL_PATH,
    _attn_implementation="eager",
    trust_remote_code=True,
    use_safetensors=True,
)
model = model.eval().cuda().to(torch.bfloat16)

PROMPT = "<image>\n<|grounding|>Convert the document to markdown. "

def strip_layout_tags(text):
    text = re.sub(r"<\|ref\|>.*?<\|/ref\|>", "", text)
    text = re.sub(r"<\|det\|>.*?<\|/det\|>", "", text)
    return text.strip()

for pdf_name in pdf_files:
    pdf_path  = os.path.join(INPUT_DIR, pdf_name)
    pdf_stem  = os.path.splitext(pdf_name)[0]
    out_dir   = os.path.join(OUTPUT_DIR, pdf_stem)
    imgs_dir  = os.path.join(out_dir, "images")
    master_md = os.path.join(out_dir, f"{pdf_stem}.md")

    if os.path.exists(master_md):
        print(f"Skipping {pdf_name} (already done)")
        continue

    os.makedirs(imgs_dir, exist_ok=True)
    print(f"\n=== {pdf_name} ===")

    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    sections = []
    img_counter = 0

    with tempfile.TemporaryDirectory() as tmp:
        # Render all pages to PNG at 300 DPI
        page_imgs = []
        for i in range(total_pages):
            mat = fitz.Matrix(300 / 72, 300 / 72)
            pix = doc[i].get_pixmap(matrix=mat)
            p = os.path.join(tmp, f"page_{i+1:04d}.png")
            pix.save(p)
            page_imgs.append(p)

        # OCR each page
        for idx, img_path in enumerate(page_imgs):
            page_num = idx + 1
            print(f"  Page {page_num}/{total_pages}...")
            page_tmp = os.path.join(tmp, f"ocr_{page_num:04d}")
            os.makedirs(page_tmp, exist_ok=True)

            model.infer(
                tokenizer,
                prompt=PROMPT,
                image_file=img_path,
                output_path=page_tmp,
                base_size=1024,
                image_size=640,
                crop_mode=True,
                save_results=True,
                test_compress=True,
            )

            mmd_path = os.path.join(page_tmp, "result.mmd")
            page_text = open(mmd_path, encoding="utf-8").read() if os.path.exists(mmd_path) else "[NO OUTPUT]"

            # Move extracted figures into the shared images/ folder, rename to avoid collisions
            src_imgs = os.path.join(page_tmp, "images")
            if os.path.isdir(src_imgs):
                for fname in sorted(os.listdir(src_imgs)):
                    ext = os.path.splitext(fname)[1]
                    new_name = f"p{page_num:04d}_{img_counter:04d}{ext}"
                    shutil.copy2(os.path.join(src_imgs, fname), os.path.join(imgs_dir, new_name))
                    page_text = page_text.replace(f"images/{fname}", f"images/{new_name}")
                    img_counter += 1

            sections.append(f"<!-- page {page_num} -->\n\n{strip_layout_tags(page_text)}")

    doc.close()

    with open(master_md, "w", encoding="utf-8") as f:
        f.write("\n\n---\n\n".join(sections))

    print(f"  -> {master_md}  ({total_pages} pages, {img_counter} images)")

print("\nDone.")
