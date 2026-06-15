## Apple Developer Docs Downloader

Download any Apple developer documentation framework as organized, readable markdown files.

## Why

Apple's developer docs are web-only and hard to search offline. This script crawls the entire documentation tree for a framework via Apple's JSON API and converts every page to clean markdown — declarations, parameters, code listings, relationships, and all. No dependencies beyond Python 3. Resumes interrupted downloads automatically.

## Quick start

```sh
python3 apple_developer_docs_downloader.py vision
```

This creates a `vision-docs/` directory with markdown files mirroring the framework's documentation hierarchy. The script prints progress as it downloads:

```
[1] Vision
[2] Vision/VNRequest
[3] Vision/VNObservation
...
```

## Usage

Pass a framework name or a full Apple docs URL:

```sh
# By name (case-insensitive)
python3 apple_developer_docs_downloader.py SwiftUI
python3 apple_developer_docs_downloader.py accessibility

# By URL
python3 apple_developer_docs_downloader.py https://developer.apple.com/documentation/Foundation

# Custom output directory
python3 apple_developer_docs_downloader.py -o my-vision-docs Vision

# Fewer workers for slower connections
python3 apple_developer_docs_downloader.py -w 4 Vision
```

If the download is interrupted, run the same command again — it picks up where it left off using saved state in the output directory.

## Configuration

All options are command-line flags:

| Flag | Default | Description |
| --- | --- | --- |
| `framework` | *(required)* | Framework name or Apple docs URL |
| `-o`, `--output` | `<framework>-docs` | Output directory |
| `-w`, `--workers` | `12` | Number of parallel download threads |

## License

No license file. All rights reserved.
