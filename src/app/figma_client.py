"""
Figma API client for fetching design file information.

This module integrates with Figma to enrich test plan context with:
- Design file metadata
- Frames and pages (screen names)
- Component definitions
- Design specifications
"""

import logging
import re

import httpx

from .config import settings
from .models import FigmaComponent, FigmaContext, FigmaFrame

logger = logging.getLogger(__name__)


class FigmaAuthError(Exception):
    """Raised when Figma returns 401 or 403 auth-related errors."""

    def __init__(self, message: str, status_code: int, error_type: str = "invalid") -> None:
        """
        Initialize FigmaAuthError.

        Args:
            message: Error message
            status_code: HTTP status code (401 or 403)
            error_type: Type of error - "invalid", "expired", "insufficient_permissions", "rate_limited"
        """
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


class FigmaClient:
    """Client for interacting with Figma API."""

    def __init__(self, token: str | None = None):
        """
        Initialize Figma client.

        Args:
            token: Figma personal access token (optional)
        """
        self.token = token or settings.figma_token
        self.base_url = "https://api.figma.com/v1"

    def _headers(self) -> dict:
        """Build headers for Figma API requests."""
        headers = {
            "Accept": "application/json",
        }
        if self.token:
            headers["X-FIGMA-TOKEN"] = self.token
        return headers

    def _parse_figma_url(self, figma_url: str) -> str | None:
        """
        Extract file key from Figma URL.

        Supports URLs like:
        - https://www.figma.com/file/{key}/...
        - https://figma.com/design/{key}/...
        - https://www.figma.com/proto/{key}/...

        Args:
            figma_url: Full Figma URL

        Returns:
            File key or None if URL is invalid
        """
        try:
            # Match Figma URLs with file, design, or proto paths
            pattern = r"figma\.com/(file|design|proto)/([A-Za-z0-9]+)"
            match = re.search(pattern, figma_url)
            if match:
                return match.group(2)
            logger.warning(f"Could not parse Figma file key from URL: {figma_url}")
            return None
        except Exception as e:
            logger.error(f"Error parsing Figma URL: {e}")
            return None

    async def fetch_file_context(self, figma_url: str) -> FigmaContext | None:
        """
        Fetch design context from Figma file.

        Args:
            figma_url: Full Figma file URL

        Returns:
            FigmaContext with file metadata, frames, and components, or None on failure
        """
        if not self.token:
            logger.warning("Figma token not configured - skipping Figma context fetch")
            return None

        file_key = self._parse_figma_url(figma_url)
        if not file_key:
            return None

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Fetch file metadata and node tree
                file_url = f"{self.base_url}/files/{file_key}"
                file_response = await client.get(file_url, headers=self._headers())

                if file_response.status_code == 404:
                    logger.warning(f"Figma file not found or no access: {figma_url}")
                    return None
                elif file_response.status_code == 403:
                    logger.warning(f"Figma API rate limit or insufficient permissions: {figma_url}")
                    return None
                elif file_response.status_code != 200:
                    logger.warning(f"Figma API returned status {file_response.status_code} for {figma_url}")
                    return None

                file_response.raise_for_status()
                file_data = file_response.json()

                # Extract file metadata
                file_name = file_data.get("name", "Unknown")
                last_modified = file_data.get("lastModified")
                version = file_data.get("version")

                # Extract frames from document tree
                document = file_data.get("document", {})
                frames = self._extract_frames(document)

                # Fetch components
                components = await self._fetch_components(client, file_key)

                logger.info(
                    f"Fetched Figma context: {file_name} "
                    f"({len(frames)} frames, {len(components)} components)"
                )

                return FigmaContext(
                    file_name=file_name,
                    file_key=file_key,
                    last_modified=last_modified,
                    frames=frames,
                    components=components,
                    version=version,
                )

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error fetching Figma file {figma_url}: {e}")
            return None
        except httpx.TimeoutException:
            logger.error(f"Timeout fetching Figma file {figma_url}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching Figma file {figma_url}: {e}")
            return None

    def _extract_frames(self, node: dict, depth: int = 0, max_depth: int = 3) -> list[FigmaFrame]:
        """
        Recursively extract frames and pages from Figma document tree.

        Args:
            node: Figma node from document tree
            depth: Current recursion depth
            max_depth: Maximum recursion depth (prevents infinite loops)

        Returns:
            List of FigmaFrame objects
        """
        frames = []

        # Stop if max depth reached
        if depth > max_depth:
            return frames

        node_type = node.get("type")
        node_name = node.get("name", "Unnamed")
        node_id = node.get("id")

        # Collect frames, pages, and components
        if node_type in ["FRAME", "COMPONENT", "PAGE", "CANVAS"]:
            frames.append(
                FigmaFrame(
                    name=node_name,
                    type=node_type,
                    node_id=node_id,
                )
            )

        # Recursively process children
        children = node.get("children", [])
        for child in children[:50]:  # Limit children to prevent overload
            frames.extend(self._extract_frames(child, depth + 1, max_depth))

        return frames[:50]  # Limit total frames to 50

    async def _fetch_components(self, client: httpx.AsyncClient, file_key: str) -> list[FigmaComponent]:
        """
        Fetch component definitions from Figma file.

        Args:
            client: HTTP client
            file_key: Figma file key

        Returns:
            List of FigmaComponent objects
        """
        try:
            components_url = f"{self.base_url}/files/{file_key}/components"
            response = await client.get(components_url, headers=self._headers())

            if response.status_code != 200:
                logger.warning(f"Failed to fetch components: status {response.status_code}")
                return []

            data = response.json()
            components = []

            # Extract component metadata
            component_metadata = data.get("meta", {}).get("components", [])

            for comp_data in component_metadata[:30]:  # Limit to 30 components
                name = comp_data.get("name", "Unnamed Component")
                description = comp_data.get("description")
                component_set_name = comp_data.get("containing_frame", {}).get("name")

                components.append(
                    FigmaComponent(
                        name=name,
                        description=description,
                        component_set_name=component_set_name,
                    )
                )

            logger.info(f"Fetched {len(components)} components from Figma file")
            return components

        except Exception as e:
            logger.warning(f"Failed to fetch Figma components: {e}")
            return []
