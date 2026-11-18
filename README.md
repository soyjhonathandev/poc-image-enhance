# POC: Mejorar con IA

Turns a phone photo of a product into an e-commerce shot (centered, white background, studio light)
with FLUX 3 Image Klein through the Black Forest Labs API, without altering the product.
Standard-library Python plus macOS `sips`; nothing to install.

## Setup

1. Create an API key at https://dashboard.bfl.ai and add credits.
2. `cp .env.example .env` and paste the key into `BFL_API_KEY`. `.env` is gitignored; the key never reaches the browser.

| Variable | Default | |
|---|---|---|
| `BFL_API_KEY` | none | Required |
| `BFL_ENDPOINT` | `https://api.bfl.ai/v1/flux-3-image-klein` | |
| `IMAGE_ENHANCE_RESOLUTION` | `1k` | `768sq`, `1k`, `1.5k`, `2k`, `4k` |
| `IMAGE_ENHANCE_ASPECT_RATIO` | `1:1` | `auto` keeps the input framing |
| `IMAGE_ENHANCE_TIMEOUT_SECONDS` | `120` | Total time per image |
| `PORT` | `8787` | Web UI port |

## Run

Web UI with the side-by-side comparison:

```sh
python3 server.py   # then open http://127.0.0.1:8787
```

CLI, one or more photos:

```sh
python3 enhance.py ~/Desktop/foto1.jpg ~/Desktop/foto2.heic
```

Originals (converted to JPEG, shrunk to 16 MP when larger) and results land in `results/`.
`results/log.jsonl` keeps one line per request: task id, final status, duration, cost and the expanded prompt.

## Cost

About US$0.009 per image at `1k`. The `cost` field BFL returns is logged per request.
