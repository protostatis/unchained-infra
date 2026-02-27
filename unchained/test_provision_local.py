#!/usr/bin/env python3
"""Manual test: provision Gemini key for protostatis via local Chrome profile.

Usage:
    uv run test_provision_local.py [--profile EMAIL] [--reset]

Flags:
    --profile EMAIL   Google account email to use (default: test@example.com)
    --reset           Revoke any stored key before provisioning
"""
import argparse
import asyncio
import os
import sys

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import signup_agent


def main():
    parser = argparse.ArgumentParser(description="Test local Gemini provisioning")
    parser.add_argument("--profile", default="test@example.com",
                        help="Google account email to use")
    parser.add_argument("--reset", action="store_true",
                        help="Revoke stored key before testing")
    args = parser.parse_args()

    # Find the Chrome profile for this email
    profiles = signup_agent.list_chrome_profiles()
    match = [p for p in profiles if p["email"] == args.profile]
    if not match:
        print(f"No Chrome profile found for {args.profile}")
        print("Available profiles:")
        for p in profiles:
            print(f"  {p['email']:40s}  {p['path']}")
        sys.exit(1)

    profile = match[0]
    profile_path = profile["path"]
    user_id = "u-test-local"

    print(f"Profile:  {profile['full_name']} ({profile['email']})")
    print(f"Path:     {profile_path}")
    print(f"User ID:  {user_id}")
    print()

    # Reset if requested
    if args.reset:
        revoked = signup_agent.revoke_provider_key(user_id, "gemini")
        print(f"Reset: {'revoked existing key' if revoked else 'no key to revoke'}")
    else:
        existing = signup_agent.get_provider_key(user_id, "gemini")
        if existing:
            print(f"Already provisioned: {existing[:10]}...{existing[-4:]}")
            print("Use --reset to revoke and re-provision")
            sys.exit(0)

    print()
    print("Starting provisioning (Chrome will open visibly)...")
    print("=" * 60)

    result = asyncio.run(signup_agent.provision_key_local(
        provider_name="gemini",
        user_id=user_id,
        profile_path=profile_path,
    ))

    print("=" * 60)
    print()
    print(f"Status:   {result.status.value}")
    print(f"Message:  {result.message}")
    print(f"Duration: {result.duration_ms}ms")
    if result.api_key:
        print(f"Key:      {result.api_key[:10]}...{result.api_key[-4:]}")

    # Verify DB storage
    stored = signup_agent.get_provider_key(user_id, "gemini")
    if stored:
        print(f"Stored:   {stored[:10]}...{stored[-4:]}  (in DB)")
    else:
        print("Stored:   NOT IN DB")

    print()
    sys.exit(0 if result.api_key else 1)


if __name__ == "__main__":
    main()
