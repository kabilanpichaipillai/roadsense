---
title: RoadSense
emoji: 🛣️
colorFrom: yellow
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# RoadSense demo

Agentic road-defect reporting: a dashcam spots damage, blurs bystanders, and AI agents triage each defect to the right
road authority. A reviewer approves the exact payload before anything is filed.

This Space runs the demo on free CPU hardware with sample data. It resets when the Space restarts, and the
"Halifax 311" it files to is a mock server inside the container: nothing reaches a real city.

Open it at the direct `*.hf.space` address; the app refuses to load inside a frame.
