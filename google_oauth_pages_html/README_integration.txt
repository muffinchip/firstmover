# How to host these pages in your Flask app

1) Place `privacy.html` and `terms.html` into a `static` folder (e.g., `static/legal/`), or serve them via templates.

## Option A: Static files
- Move files to `static/legal/` in your project.
- In Google OAuth consent screen, set:
  - Privacy Policy URL: `https://<your-domain>/static/legal/privacy.html`
  - Terms of Service URL: `https://<your-domain>/static/legal/terms.html`

## Option B: Template routes
Add these routes to your `app.py`:

```python
from flask import Flask, render_template

@app.route("/privacy")
def privacy():
    return app.send_static_file("legal/privacy.html")

@app.route("/terms")
def terms():
    return app.send_static_file("legal/terms.html")
```

Then place the HTML files in `static/legal/`.
