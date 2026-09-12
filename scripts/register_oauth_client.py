#!/usr/bin/env python3
"""
Script to register OAuth clients for QTable
Run this after starting the server to register your Tauri app client
"""

import asyncio
import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.oauth import OAuthClient


async def register_tauri_client():
    """Register the Tauri app as an OAuth client"""
    
    async with AsyncSessionLocal() as session:
        # Check if client already exists
        result = await session.execute(
            select(OAuthClient).where(OAuthClient.client_id == "tauri_app")
        )
        existing_client = result.scalars().first()
        
        if existing_client:
            print("Tauri client already registered:")
            print(f"  Client ID: {existing_client.client_id}")
            print(f"  Redirect URIs: {existing_client.redirect_uris}")
            return
        
        # Create new client
        client = OAuthClient(
            client_id="tauri_app",
            client_name="QTable Tauri App",
            redirect_uris="qtable://login/callback,http://localhost:9100/oauth/callback",
            scope="read write",
            is_active=True,
        )
        
        session.add(client)
        await session.commit()
        await session.refresh(client)
        
        print("Tauri client registered successfully!")
        print(f"  Client ID: {client.client_id}")
        print(f"  Client Name: {client.client_name}")
        print(f"  Redirect URIs: {client.redirect_uris}")
        print(f"  Scope: {client.scope}")
        print("\nUse this client_id in your Tauri app OAuth configuration.")


async def list_clients():
    """List all registered OAuth clients"""
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(OAuthClient))
        clients = result.scalars().all()
        
        if not clients:
            print("No OAuth clients registered.")
            return
        
        print("Registered OAuth Clients:")
        print("-" * 60)
        for client in clients:
            print(f"Client ID: {client.client_id}")
            print(f"Client Name: {client.client_name}")
            print(f"Redirect URIs: {client.redirect_uris}")
            print(f"Scope: {client.scope}")
            print(f"Active: {client.is_active}")
            print("-" * 60)


async def main():
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "register":
            await register_tauri_client()
        elif command == "list":
            await list_clients()
        else:
            print("Usage: python register_oauth_client.py [register|list]")
    else:
        # Default: register Tauri client
        await register_tauri_client()


if __name__ == "__main__":
    asyncio.run(main())
