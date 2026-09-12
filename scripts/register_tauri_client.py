#!/usr/bin/env python3
"""
Register Tauri desktop app as an OAuth client in QTable
"""
import asyncio
import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.oauth import OAuthClient


async def register_tauri_client():
    """Register the Tauri desktop app as an OAuth client"""
    
    # Check if client already exists
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(OAuthClient).where(OAuthClient.client_id == "tauri_app")
        )
        existing_client = result.scalars().first()
        
        if existing_client:
            print("✅ Tauri client already registered:")
            print(f"   Client ID: {existing_client.client_id}")
            print(f"   Redirect URIs: {existing_client.redirect_uris}")
            print(f"   Active: {existing_client.is_active}")
            return
        
        # Create new client
        tauri_client = OAuthClient(
            client_id="tauri_app",
            client_name="Cloud Design Client (Tauri Desktop App)",
            # qtable:// for deep-link (production), http://localhost for local HTTP callback (dev)
            redirect_uris="qtable://login/callback,qtable://oauth/callback,http://localhost:1420/oauth/callback",
            scope="read write",
            is_active=True,
        )
        
        db.add(tauri_client)
        await db.commit()
        await db.refresh(tauri_client)
        
        print("✅ Successfully registered Tauri OAuth client:")
        print(f"   Client ID: {tauri_client.client_id}")
        print(f"   Client Name: {tauri_client.client_name}")
        print(f"   Redirect URI: {tauri_client.redirect_uris}")
        print(f"   Scope: {tauri_client.scope}")
        print("\n📝 You can now use this client for OAuth authentication")


if __name__ == "__main__":
    try:
        asyncio.run(register_tauri_client())
    except Exception as e:
        print(f"❌ Error registering client: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
