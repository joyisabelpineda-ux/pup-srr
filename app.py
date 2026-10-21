"""Local entry point:  python app.py   ->  http://127.0.0.1:5000"""
import os

from dotenv import load_dotenv

load_dotenv()  # read .env (if present) before the app reads its settings

from srr import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  PUP Smart Room Reservation System\n  Open  http://127.0.0.1:{port}  in your browser (Ctrl+C to stop)\n")
    if app.config["PRODUCTION"]:
        from waitress import serve
        serve(app, host=host, port=port, threads=4)
    else:
        app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
