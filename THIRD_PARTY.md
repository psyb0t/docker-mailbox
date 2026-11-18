# Third-Party Components

This project's own source is licensed under the AGPL-3.0-only (see [`LICENSE`](LICENSE)). The published
Docker image (`psyb0t/mailbox`) additionally bundles the third-party
components below as runtime dependencies. Their licenses apply to those
components, not to this project's source.

| Component | Kind | License | Source | Where it lives | Note |
| --- | --- | --- | --- | --- | --- |
| [html2text](https://github.com/Alir3z4/html2text) | runtime dependency | GPL-3.0 | https://github.com/Alir3z4/html2text | Installed into the published Docker image's Python virtualenv (`pyproject.toml` dependency, imported by `src/mailboxd/imap_client.py`) | GPL. runtime dependency installed into the published Docker image; the image combines it with this AGPL-3.0-only project, which section 13 of both licenses permits. Corresponding source at the URL above. Full license text: [`LICENSES/GPL-3.0.txt`](LICENSES/GPL-3.0.txt). |
| [nh3](https://github.com/messense/nh3) | runtime dependency | MIT | https://github.com/messense/nh3 | Installed into the image's Python virtualenv and used by `src/mailboxd/mime.py` | Python bindings to the ammonia HTML sanitizer. Distributed wheel license notices apply to bundled native components. |
