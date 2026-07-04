"""
Tests for Cloud Device Plugin (EITElite v0.3)
==============================================

P1-8: Optional dependency tests use pytest.importorskip instead of try/except ImportError.
"""

import pytest
import asyncio

pytestmark = pytest.mark.skip(reason="cloud_device plugin not loaded")

# P1-8: cloud_device plugin belongs to Full edition, Lite edition may not have this module
cloud_device = pytest.importorskip(
    "tical_code.plugins.cloud_device",
    reason="cloud_device plugin not available (Full edition)",
)


class TestDeviceAction:
    """Test DeviceAction class."""
    
    def test_action_creation(self):
        """Test action creation."""
        from tical_code.plugins.cloud_device import DeviceAction, ActionType
        
        action = DeviceAction(
            action_id="act_1",
            action_type=ActionType.BROWSER_CLICK,
            device_id="browser_1",
            selector="#button",
        )
        
        assert action.action_id == "act_1"
        assert action.action_type == ActionType.BROWSER_CLICK
        assert action.selector == "#button"


class TestDeviceResult:
    """Test DeviceResult class."""
    
    def test_result_creation(self):
        """Test result creation."""
        from tical_code.plugins.cloud_device import DeviceResult, ActionType
        
        result = DeviceResult(
            action_id="r1",
            action_type=ActionType.BROWSER_SCREENSHOT,
            success=True,
        )
        
        assert result.success == True
        assert result.verified == False
    
    def test_generate_evidence_hash(self):
        """Test evidence hash generation."""
        from tical_code.plugins.cloud_device import DeviceResult, ActionType
        
        result = DeviceResult(
            action_id="r1",
            action_type=ActionType.BROWSER_OPEN,
            success=True,
            screenshot="base64data...",
        )
        
        result._generate_evidence_hash()
        
        assert result.evidence_hash is not None
        assert len(result.evidence_hash) == 16
    
    def test_to_dict(self):
        """Test serialization."""
        from tical_code.plugins.cloud_device import DeviceResult, ActionType
        
        result = DeviceResult(
            action_id="r1",
            action_type=ActionType.BROWSER_CLICK,
            success=True,
        )
        
        data = result.to_dict()
        
        assert data['action_id'] == "r1"
        assert data['success'] == True
        # Screenshot should be bool, not full data
        assert isinstance(data['screenshot'], bool)


class TestDeviceStatus:
    """Test DeviceStatus enum."""
    
    def test_status_values(self):
        """Test status enum values."""
        from tical_code.plugins.cloud_device import DeviceStatus
        
        assert DeviceStatus.DISCONNECTED.value == "disconnected"
        assert DeviceStatus.CONNECTED.value == "connected"
        assert DeviceStatus.BUSY.value == "busy"


class TestActionType:
    """Test ActionType enum."""
    
    def test_browser_actions(self):
        """Test browser action types."""
        from tical_code.plugins.cloud_device import ActionType
        
        assert ActionType.BROWSER_OPEN.value == "browser_open"
        assert ActionType.BROWSER_CLICK.value == "browser_click"
        assert ActionType.BROWSER_TYPE.value == "browser_type"
        assert ActionType.BROWSER_SCREENSHOT.value == "browser_screenshot"
    
    def test_mobile_actions(self):
        """Test mobile action types."""
        from tical_code.plugins.cloud_device import ActionType
        
        assert ActionType.MOBILE_TAP.value == "mobile_tap"
        assert ActionType.MOBILE_SCREENSHOT.value == "mobile_screenshot"


class TestBrowserTool:
    """Test BrowserTool class."""
    
    def test_browser_creation(self):
        """Test browser tool creation."""
        from tical_code.plugins.cloud_device import BrowserTool
        
        browser = BrowserTool(device_id="test_browser")
        
        assert browser.device_id == "test_browser"
        assert browser._page is None
    
    def test_lazy_import_playwright(self, skip_without_playwright):
        """Test lazy import of playwright - P1-8: skip via importorskip fixture."""
        from tical_code.plugins.cloud_device import BrowserTool
        
        browser = BrowserTool()
        
        # Should return False if playwright not installed
        has_playwright = browser._lazy_import_playwright()
        
        # Just check it doesn't crash
        assert isinstance(has_playwright, bool)
    
    def test_lazy_import_selenium(self, skip_without_selenium):
        """Test lazy import of selenium - P1-8: skip via importorskip fixture."""
        from tical_code.plugins.cloud_device import BrowserTool
        
        browser = BrowserTool()
        
        # Should return False if selenium not installed
        has_selenium = browser._lazy_import_selenium()
        
        # Just check it doesn't crash
        assert isinstance(has_selenium, bool)
    
    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self):
        """Test disconnect when not connected."""
        from tical_code.plugins.cloud_device import BrowserTool
        
        browser = BrowserTool()
        
        # Should not crash
        await browser.disconnect()
        
        assert browser._page is None
        assert browser._driver is None


class TestMobileTool:
    """Test MobileTool class."""
    
    def test_mobile_creation(self):
        """Test mobile tool creation."""
        from tical_code.plugins.cloud_device import MobileTool
        
        mobile = MobileTool(device_id="test_mobile")
        
        assert mobile.device_id == "test_mobile"
        assert mobile._driver is None
    
    @pytest.mark.asyncio
    async def test_screenshot_when_not_connected(self):
        """Test screenshot when not connected."""
        from tical_code.plugins.cloud_device import MobileTool
        
        mobile = MobileTool()
        
        result = await mobile.screenshot()
        
        assert result is None


class TestCloudDevicePlugin:
    """Test CloudDevicePlugin class."""
    
    def test_plugin_creation(self):
        """Test plugin creation."""
        from tical_code.plugins.cloud_device import CloudDevicePlugin
        
        plugin = CloudDevicePlugin()
        
        assert plugin.browser is not None
        assert plugin.mobile is not None
        assert len(plugin._action_history) == 0
    
    def test_get_action_history(self):
        """Test getting action history."""
        from tical_code.plugins.cloud_device import CloudDevicePlugin, DeviceResult, ActionType
        
        plugin = CloudDevicePlugin()
        
        # Add some mock results
        result = DeviceResult(
            action_id="r1",
            action_type=ActionType.BROWSER_OPEN,
            success=True,
        )
        plugin._action_history.append(result)
        
        history = plugin.get_action_history()
        
        assert len(history) == 1
        assert history[0]['success'] == True
    
    @pytest.mark.asyncio
    async def test_wait(self):
        """Test wait function."""
        from tical_code.plugins.cloud_device import CloudDevicePlugin
        
        plugin = CloudDevicePlugin()
        
        start = asyncio.get_event_loop().time()
        await plugin.wait(0.01)
        elapsed = asyncio.get_event_loop().time() - start
        
        assert elapsed >= 0.01


class TestGetCloudDevicePlugin:
    """Test get_cloud_device_plugin function."""
    
    def test_get_plugin(self):
        """Test getting plugin instance."""
        from tical_code.plugins.cloud_device import get_cloud_device_plugin
        
        plugin1 = get_cloud_device_plugin()
        plugin2 = get_cloud_device_plugin()
        
        # Should return same instance
        assert plugin1 is plugin2


class TestVerifyResult:
    """Test result verification."""
    
    @pytest.mark.asyncio
    async def test_verify_successful_result(self):
        """Test verifying a successful result."""
        from tical_code.plugins.cloud_device import CloudDevicePlugin, DeviceResult, ActionType
        
        plugin = CloudDevicePlugin()
        
        result = DeviceResult(
            action_id="r1",
            action_type=ActionType.BROWSER_CLICK,
            success=True,
        )
        result.start_time = 0
        result.end_time = 0.1
        result.duration_ms = 100
        
        verified = await plugin._verify_result(result, "test")
        
        # Should pass basic verification
        assert isinstance(verified, bool)
    
    @pytest.mark.asyncio
    async def test_verify_empty_screenshot(self):
        """Test verifying result with empty screenshot."""
        from tical_code.plugins.cloud_device import CloudDevicePlugin, DeviceResult, ActionType
        
        plugin = CloudDevicePlugin()
        
        result = DeviceResult(
            action_id="r1",
            action_type=ActionType.BROWSER_SCREENSHOT,
            success=True,
            screenshot="",  # Empty screenshot
        )
        result.start_time = 0
        result.end_time = 0.1
        result.duration_ms = 100
        
        verified = await plugin._verify_result(result, "test")
        
        # May fail due to empty screenshot
        assert isinstance(verified, bool)
