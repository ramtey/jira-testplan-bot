#!/bin/bash
# Installation script for testplan CLI
set -e

echo "🚀 Installing testplan CLI..."
echo ""

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "📦 uv not found. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh

    # Add uv to current PATH
    export PATH="$HOME/.cargo/bin:$PATH"

    echo "✓ uv installed successfully"
    echo ""
fi

# Install testplan CLI
echo "📦 Installing testplan CLI from repository..."
uv tool install git+https://github.com/$(git config --get remote.origin.url | sed 's/.*github.com[:/]\(.*\)\.git/\1/')

echo ""
echo "✅ Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Run 'testplan setup' to configure your API tokens"
echo "  2. Run 'testplan --help' to see available commands"
echo "  3. Try 'testplan generate YOUR-TICKET-123'"
echo ""
echo "To update later, run: uv tool upgrade testplan"
