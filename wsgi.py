"""WSGI entry point for production servers:  waitress-serve --host=0.0.0.0 --port=$PORT wsgi:app"""
from dotenv import load_dotenv

load_dotenv()

from srr import create_app  # noqa: E402

app = create_app()
