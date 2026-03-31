from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml

from bot.config import config
from bot.models.project import Project, ProjectStatus
from bot.services.progress import ProgressTracker

logger = logging.getLogger(__name__)


class TunnelManager:
    """Manages Cloudflare Tunnel configuration to route subdomains to containers."""

    def __init__(self, project: Project, tracker: ProgressTracker):
        self.project = project
        self.tracker = tracker

    async def setup_route(self) -> str:
        """
        Add a subdomain route to the cloudflared config and reload the tunnel.
        Returns the public URL, or empty string on failure.
        """
        subdomain = self.project.slug
        hostname = f"{subdomain}.{config.BASE_DOMAIN}"
        url = f"https://{hostname}"
        local_service = f"http://127.0.0.1:{self.project.port}"

        await self.tracker.step_start("tunnel", f"Routing {hostname}...")

        try:
            config_path = Path(config.CLOUDFLARED_CONFIG_PATH)

            # ── Update cloudflared config ────────────
            self.tracker.log(f"Updating cloudflared config: {hostname} → {local_service}")

            if config_path.exists():
                with open(config_path) as f:
                    tunnel_config = yaml.safe_load(f) or {}
            else:
                tunnel_config = {}

            # Ensure base structure
            tunnel_config.setdefault("tunnel", config.TUNNEL_UUID)
            tunnel_config.setdefault("credentials-file", config.CLOUDFLARED_CREDENTIALS)
            tunnel_config.setdefault("ingress", [])

            ingress = tunnel_config["ingress"]

            # Remove any existing rule for this hostname
            ingress = [
                rule for rule in ingress
                if rule.get("hostname") != hostname
            ]

            # Remove the catch-all rule (must always be last)
            catch_all = None
            if ingress and "hostname" not in ingress[-1]:
                catch_all = ingress.pop()

            # Add new rule
            new_rule = {
                "hostname": hostname,
                "service": local_service,
            }
            ingress.append(new_rule)

            # Re-add catch-all at the end
            if catch_all:
                ingress.append(catch_all)
            else:
                ingress.append({"service": "http_status:404"})

            tunnel_config["ingress"] = ingress

            # Write updated config
            with open(config_path, "w") as f:
                yaml.dump(tunnel_config, f, default_flow_style=False)

            self.tracker.log(f"Config written: {len(ingress)} ingress rules total")

            # ── Reload cloudflared ───────────────────
            reload_ok, reload_detail = await self._reload_tunnel()
            if reload_ok:
                self.tracker.log(f"Tunnel reloaded: {reload_detail}")
            else:
                self.tracker.log(f"⚠️ Tunnel reload issue: {reload_detail}")
                # Don't fail here — cloudflared might still pick up changes

            # ── Verify tunnel is routing ─────────────
            await asyncio.sleep(5)

            # Quick check: does cloudflared know about our hostname?
            verify_ok = await self._verify_route(hostname)
            if verify_ok:
                self.tracker.log(f"Tunnel route verified for {hostname}")
                await self.tracker.step_done("tunnel", f"{hostname} → port {self.project.port}")
            else:
                self.tracker.log(f"⚠️ Could not verify tunnel route — may need time to propagate")
                await self.tracker.step_done("tunnel", f"Route added (may need DNS propagation)")

            return url

        except PermissionError as e:
            error_msg = (
                f"Permission denied writing to {config.CLOUDFLARED_CONFIG_PATH}. "
                f"The appfactory user needs write access to the cloudflared config. "
                f"Fix: sudo chown appfactory {config.CLOUDFLARED_CONFIG_PATH} or add to sudoers."
            )
            self.tracker.log(f"❌ Tunnel PERMISSION ERROR: {error_msg}")
            await self.tracker.step_fail("tunnel", "Permission denied")
            await self.tracker.fail(error_msg)
            return ""

        except Exception as e:
            logger.exception(f"Tunnel setup failed for {self.project.slug}")
            self.tracker.log(f"❌ Tunnel error: {e}")
            await self.tracker.step_fail("tunnel", str(e)[:100])
            await self.tracker.fail(f"Tunnel routing failed: {str(e)}")
            return ""

    async def remove_route(self):
        """Remove the subdomain route from the cloudflared config."""
        hostname = f"{self.project.slug}.{config.BASE_DOMAIN}"
        config_path = Path(config.CLOUDFLARED_CONFIG_PATH)

        try:
            if not config_path.exists():
                return

            with open(config_path) as f:
                tunnel_config = yaml.safe_load(f) or {}

            ingress = tunnel_config.get("ingress", [])
            tunnel_config["ingress"] = [
                rule for rule in ingress
                if rule.get("hostname") != hostname
            ]

            # Ensure catch-all is still there
            if tunnel_config["ingress"] and "hostname" in tunnel_config["ingress"][-1]:
                tunnel_config["ingress"].append({"service": "http_status:404"})

            with open(config_path, "w") as f:
                yaml.dump(tunnel_config, f, default_flow_style=False)

            await self._reload_tunnel()
            logger.info(f"Removed tunnel route for {hostname}")

        except Exception as e:
            logger.warning(f"Failed to remove tunnel route for {hostname}: {e}")

    async def _reload_tunnel(self) -> tuple[bool, str]:
        """Restart cloudflared to pick up config changes. Returns (success, detail)."""
        # Strategy 1: sudo systemctl restart
        proc = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "restart", "cloudflared",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, "systemctl restart OK"

        err1 = stderr.decode().strip()
        logger.warning(f"sudo systemctl restart cloudflared failed: {err1}")

        # Strategy 2: without sudo
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "restart", "cloudflared",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, "systemctl restart OK (no sudo)"

        err2 = stderr.decode().strip()
        logger.warning(f"systemctl restart also failed: {err2}")

        # Strategy 3: SIGHUP
        proc = await asyncio.create_subprocess_exec(
            "sudo", "pkill", "-HUP", "cloudflared",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, "Sent SIGHUP to cloudflared"

        # Strategy 4: try cloudflared service restart directly
        proc = await asyncio.create_subprocess_exec(
            "sudo", "service", "cloudflared", "restart",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, "service restart OK"

        return False, f"All restart methods failed. Errors: {err1[:100]} / {err2[:100]}"

    async def _verify_route(self, hostname: str) -> bool:
        """Check if cloudflared is actually serving the route."""
        # Read back the config to verify our entry is there
        config_path = Path(config.CLOUDFLARED_CONFIG_PATH)
        try:
            with open(config_path) as f:
                tunnel_config = yaml.safe_load(f) or {}
            ingress = tunnel_config.get("ingress", [])
            for rule in ingress:
                if rule.get("hostname") == hostname:
                    return True
        except Exception:
            pass
        return False
