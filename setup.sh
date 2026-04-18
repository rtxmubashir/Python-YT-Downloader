#!/bin/bash

echo "🔧 Setting up YouTube Downloader..."

# Install venv if not present
if ! dpkg -l | grep -q python3.*-venv; then
    echo "📦 Installing python3-venv..."
    sudo apt update
    sudo apt install -y python3.12-venv || sudo apt install -y python3-venv
fi

# Remove old venv if exists
rm -rf venv

# Create new virtual environment
echo "📁 Creating virtual environment..."
python3 -m venv venv

# Activate and install packages
echo "📥 Installing packages..."
source venv/bin/activate
pip install --upgrade pip
pip install customtkinter yt-dlp

echo "✅ Setup complete!"
echo ""
echo "To run the application:"
echo "  source venv/bin/activate"
echo "  python main.py"