"""A minimal, generic mock site: a trust-center-style page listing N
downloadable documents. Deliberately NOT part of src/cua/target_app/ (the
CU banking app) -- this exists only to validate that repeat_for_each
generalizes across *any* site shape, by resizing the document count
between runs without re-recording anything.

Usage: python -m tests.fixtures.doc_list_app --count 3 --port 5200
"""

from __future__ import annotations

import argparse

from flask import Flask, Response, redirect, render_template_string, request, session, url_for

_PAGE = """
<html><body>
<h1>Acme Corp Trust Center</h1>
<p>Logged in as: {{ session['username'] }}</p>
<ul>
{% for doc in docs %}
  <li><a href="/trust-center/documents/{{ doc }}.pdf">{{ doc }} report (PDF)</a></li>
{% endfor %}
</ul>
</body></html>
"""

_LOGIN_PAGE = """
<html><body>
<form method="post">
  <input name="username" placeholder="username">
  <input name="password" type="password" placeholder="password">
  <button type="submit">Log in</button>
</form>
</body></html>
"""


def create_app(num_docs: int) -> Flask:
    app = Flask(__name__)
    app.secret_key = "dev-only-fixture-key"
    docs = [f"soc2-type2" if i == 0 else f"iso27001-cert" if i == 1 else f"pentest-summary-{i}" for i in range(num_docs)]

    @app.route("/healthz")
    def healthz():
        return "OK", 200

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            session["username"] = request.form.get("username", "")
            return redirect(url_for("trust_center"))
        return render_template_string(_LOGIN_PAGE)

    @app.route("/trust-center")
    def trust_center():
        if "username" not in session:
            return redirect(url_for("login"))
        return render_template_string(_PAGE, docs=docs)

    @app.route("/trust-center/documents/<name>.pdf")
    def download(name):
        if "username" not in session:
            return redirect(url_for("login"))
        content = f"Synthetic compliance document content for {name}.\nNo real content.\n".encode()
        return Response(
            content, mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'},
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--port", type=int, default=5200)
    args = parser.parse_args()
    app = create_app(args.count)
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
