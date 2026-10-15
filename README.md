# NextPlay Social Studio

I built Social Studio to keep video editing and post preparation in the same workspace. I can edit a clip, prepare channel specific copy, review it, and schedule approved posts.

I included a timeline editor, a content library, caption preparation, export packages, and publishing adapters. Approval is tied to the selected video and copy. A post stays blocked when a required connection or review step is missing.

## Built with

I used Python, SQLite, JavaScript, and FFmpeg.

## Inside the project

* `source/pipeline_core/`: the video editor and processing tools.
* `source/social_platform/`: the social workspace and publishing services.
* `source/social_platform/tests/`: publishing and platform checks.
* `Dockerfile`: the container setup.

## Running it

I use Python 3.11 or later and install `source/requirements-platform.txt` in a Python environment. From `source/`, I run `python3 -m social_platform.server --port 8770` and open `http://127.0.0.1:8770`. Video processing also needs FFmpeg.

I configure my own services for AI and publishing features. The detailed guide explains the difference between implemented adapters and connections that still need live account setup. I keep the [development repository](https://github.com/2MilinEcom/nextplay-social-studio) separate from this portfolio copy.

## More detail

* [Workflow and integration setup](README_SOCIAL_STUDIO.md)
* [Deployment and verification notes](docs/history/README.md)
