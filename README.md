# HR Onboarding

Employee onboarding portal built with Streamlit. It uses Tesseract OCR to pull details from uploaded documents.

## Requirements

- Python 3.15 (managed with [uv](https://docs.astral.sh/uv/))
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki). By default the app expects it at
  `C:\Users\ankit.kumar\AppData\Local\Tesseract-OCR\tesseract.exe`. To use a different install, set the `TESSERACT_CMD` environment variable.

## Setup

```powershell
uv sync
```

## Run

```powershell
uv run streamlit run app.py
```

## Templates

The `Data/` folder holds the onboarding document templates: the joining form, the NDA and the statutory declaration.
