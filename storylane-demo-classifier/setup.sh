#!/bin/bash
# Setup script for Storylane Demo Classifier
# Run this once: bash setup.sh

echo "🎬 Setting up Storylane Demo Classifier..."
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required but not installed."
    echo "   Install it from https://www.python.org/downloads/"
    exit 1
fi

echo "✅ Python 3 found: $(python3 --version)"

# Create virtual environment
echo ""
echo "📦 Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install dependencies
echo "📦 Installing dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# Install Playwright browsers
echo "🌐 Installing browser for Playwright (this may take a minute)..."
python3 -m playwright install chromium

# Make the Start Classifier.command file runnable on macOS
# (ZIP downloads from GitHub strip executable permissions and add quarantine)
chmod +x "Start Classifier.command" 2>/dev/null
xattr -d com.apple.quarantine "Start Classifier.command" 2>/dev/null

echo ""
echo "✅ Setup complete!"
echo ""
echo "To run the tool:"
echo "  Double-click 'Start Classifier.command'"
echo "  (Your API key will be entered in the browser UI)"
echo ""
echo "Or from the terminal:"
echo "  source venv/bin/activate"
echo "  python3 app.py"
echo ""
echo "Options (terminal only):"
echo "  python3 run.py --limit 5        # Process only first 5 demos"
echo "  python3 run.py --headed          # See the browser while it works"
echo "  python3 run.py --scrape-only     # Just get the list of demo URLs"
echo "  python3 run.py --no-classify     # Walk demos but skip AI classification"
