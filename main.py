"""The brief's entry point: python main.py --invoice_path=data/invoices/invoice_1001.txt

API keys can live in a .env file next to this one (see .env.example); a variable already set in the
environment wins. With XAI_API_KEY the model is Grok; DEEPSEEK_API_KEY adds DeepSeek as the backup."""

from pathlib import Path

from dotenv import load_dotenv

from invoice_workflow.__main__ import main

if __name__ == '__main__':
    load_dotenv(Path(__file__).with_name('.env'))
    main()
