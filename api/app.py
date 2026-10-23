"""
Minimal Flask application for the exam-portal upload endpoint.

This wires up the upload blueprint. In production, this would be
integrated into the existing exam-portal application server rather
than running standalone.
"""

import logging
from pathlib import Path

from flask import Flask, render_template, request, g

from routes.upload import upload_bp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent / "templates"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB
    app.register_blueprint(upload_bp, url_prefix="/api")

    # ----- Instructor-facing page at /upload (not behind /api) -----
    # The upload_bp blueprint handles the POST at /api/upload.
    # The page itself lives here, at the root, where the exam portal
    # UI actually navigates to.
    @app.route("/upload", methods=["GET"])
    def upload_page():
        # In production these come from session/auth middleware on g.
        # For the pilot, accept query-string overrides so the page
        # can be tested without a full auth stack.
        user_id = getattr(g, "user_id", None) or request.args.get("user_id", "unknown")
        user_role = getattr(g, "user_role", None) or request.args.get("user_role", "instructor")
        session_id = getattr(g, "session_id", None) or request.args.get("session_id")

        return render_template(
            "upload.html",
            user_id=user_id,
            user_role=user_role,
            session_id=session_id,
        )

    # ----- Teacher "Create Template" page -----
    # This is the page teachers actually use to upload exam documents.
    # The OCR-LLM pilot checkbox lives HERE (not on /upload).
    @app.route("/teacher/templates/create", methods=["GET"])
    def teacher_create():
        user_id = getattr(g, "user_id", None) or request.args.get("user_id", "unknown")
        user_role = getattr(g, "user_role", None) or request.args.get("user_role", "instructor")
        session_id = getattr(g, "session_id", None) or request.args.get("session_id")

        return render_template(
            "teacher_create.html",
            user_id=user_id,
            user_role=user_role,
            session_id=session_id,
        )

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
