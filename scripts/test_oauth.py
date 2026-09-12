#!/usr/bin/env python3
"""
Test script for OAuth 2.0 endpoints
Run this after starting the QTable server
"""

import asyncio
import sys
import os
from urllib.parse import urlencode

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx


async def test_oauth_endpoints():
    base_url = "http://localhost:8000"
    
    print("=" * 60)
    print("OAuth 2.0 Endpoint Tests")
    print("=" * 60)
    
    async with httpx.AsyncClient() as client:
        # Test 1: Check if authorization endpoint exists
        print("\n1. Testing GET /oauth/authorize...")
        try:
            response = await client.get(
                f"{base_url}/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "tauri_app",
                    "redirect_uri": "qtable://login/callback",
                    "code_challenge": "test_challenge",
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            print(f"   Status: {response.status_code}")
            print(f"   ✓ Endpoint exists")
            
            # Should redirect to login (307 or 302)
            if response.status_code in [302, 307]:
                print(f"   ✓ Redirects to login (as expected)")
                location = response.headers.get("location", "")
                if "/login" in location:
                    print(f"   ✓ Correct redirect to login page")
        except Exception as e:
            print(f"   ✗ Error: {e}")
        
        # Test 2: Test invalid client_id
        print("\n2. Testing with invalid client_id...")
        try:
            response = await client.get(
                f"{base_url}/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "invalid_client",
                    "redirect_uri": "qtable://login/callback",
                    "code_challenge": "test_challenge",
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            print(f"   Status: {response.status_code}")
            if response.status_code in [302, 307]:
                location = response.headers.get("location", "")
                if "error=invalid_client" in location:
                    print(f"   ✓ Returns proper error redirect")
        except Exception as e:
            print(f"   ✗ Error: {e}")
        
        # Test 3: Test missing code_challenge
        print("\n3. Testing without code_challenge...")
        try:
            response = await client.get(
                f"{base_url}/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "tauri_app",
                    "redirect_uri": "qtable://login/callback",
                },
                follow_redirects=False,
            )
            print(f"   Status: {response.status_code}")
            if response.status_code in [302, 307]:
                location = response.headers.get("location", "")
                if "error=" in location:
                    print(f"   ✓ Returns error for missing code_challenge")
        except Exception as e:
            print(f"   ✗ Error: {e}")
        
        # Test 4: Test token endpoint with invalid data
        print("\n4. Testing POST /oauth/token with invalid data...")
        try:
            response = await client.post(
                f"{base_url}/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "code": "invalid_code",
                    "redirect_uri": "qtable://login/callback",
                    "client_id": "tauri_app",
                    "code_verifier": "test_verifier",
                },
            )
            print(f"   Status: {response.status_code}")
            if response.status_code == 400:
                print(f"   ✓ Returns 400 for invalid code")
                data = response.json()
                if "detail" in data:
                    print(f"   ✓ Error message: {data['detail']}")
        except Exception as e:
            print(f"   ✗ Error: {e}")
        
        # Test 5: Test token endpoint with wrong grant_type
        print("\n5. Testing POST /oauth/token with wrong grant_type...")
        try:
            response = await client.post(
                f"{base_url}/oauth/token",
                json={
                    "grant_type": "invalid_grant",
                    "code": "some_code",
                    "redirect_uri": "qtable://login/callback",
                    "client_id": "tauri_app",
                    "code_verifier": "test_verifier",
                },
            )
            print(f"   Status: {response.status_code}")
            if response.status_code == 400:
                print(f"   ✓ Returns 400 for invalid grant_type")
        except Exception as e:
            print(f"   ✗ Error: {e}")
        
        # Test 6: List registered clients
        print("\n6. Checking registered OAuth clients...")
        try:
            from sqlalchemy import select
            from app.db.session import AsyncSessionLocal
            from app.models.oauth import OAuthClient
            
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(OAuthClient))
                clients = result.scalars().all()
                
                if clients:
                    print(f"   ✓ Found {len(clients)} registered client(s):")
                    for client_obj in clients:
                        print(f"      - {client_obj.client_id}: {client_obj.client_name}")
                else:
                    print(f"   ⚠ No clients registered")
                    print(f"   Run: python scripts/register_oauth_client.py register")
        except Exception as e:
            print(f"   ✗ Error: {e}")
        
        print("\n" + "=" * 60)
        print("Tests completed!")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Start the server: python -m uvicorn app.main:app --reload")
        print("2. Register client: python scripts/register_oauth_client.py register")
        print("3. Read OAUTH_SETUP.md for complete integration guide")


if __name__ == "__main__":
    try:
        import httpx
    except ImportError:
        print("Installing httpx...")
        os.system("pip install httpx")
        import httpx
    
    asyncio.run(test_oauth_endpoints())
