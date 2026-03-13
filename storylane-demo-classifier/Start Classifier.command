#!/bin/bash
# Double-click this file to launch the Storylane Demo Classifier
cd "$(dirname "$0")"
source venv/bin/activate
export ANTHROPIC_API_KEY="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"
python3 app.py
