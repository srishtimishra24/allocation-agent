#!/usr/bin/env bash
pkill -f "services.publish_service:app" 2>/dev/null || true
pkill -f "services.spend_service:app"   2>/dev/null || true
pkill -f "services.bind_service:app"    2>/dev/null || true
echo "stopped"
