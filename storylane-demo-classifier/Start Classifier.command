#!/bin/bash
# Double-click this file to launch the Storylane Demo Classifier
# It will automatically set up the virtual environment on first run.

cd "$(dirname "$0")"

# --- Auto-setup: create venv and install deps if missing ---
if [ ! -d "venv" ]; then
    echo "🎬 First run detected — setting up environment..."
    echo ""

    # Check Python
    if ! command -v python3 &> /dev/null; then
        echo "❌ Python 3 is required but not installed."
        echo "   Install it from https://www.python.org/downloads/"
        echo ""
        echo "Press any key to close..."
        read -n 1
        exit 1
    fi

    echo "✅ Python 3 found: $(python3 --version)"

    echo "📦 Creating virtual environment..."
    python3 -m venv venv
    if [ $? -ne 0 ]; then
        echo "❌ Failed to create virtual environment."
        echo "Press any key to close..."
        read -n 1
        exit 1
    fi

    source venv/bin/activate

    echo "📦 Installing dependencies..."
    pip install --upgrade pip -q
    pip install -r requirements.txt -q

    echo "🌐 Installing browser for Playwright (this may take a minute)..."
    python3 -m playwright install chromium

    echo ""
    echo "✅ Setup complete! Starting the classifier..."
    echo ""
else
    source venv/bin/activate
fi

# Launch the web UI (API key is entered in the browser)
python3 app.py
