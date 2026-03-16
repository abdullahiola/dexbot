"""
DexScreener Automation Telegram Bot
Automates token listing on DexScreener and returns payment QR code link

Install dependencies:
    pip install python-telegram-bot playwright pillow zxing-cpp
    playwright install chromium
"""

import asyncio
import os
import re
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from playwright.async_api import async_playwright
from PIL import Image
import zxingcpp

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
(
    MAIN_MENU,
    RESIZE_IMAGE,
    RESIZE_TYPE,
    CHAIN,
    TOKEN_ADDRESS,
    DESCRIPTION,
    WEBSITE,
    X_URL,
    TELEGRAM_URL,
    ICON_IMAGE,
    HEADER_IMAGE,
    CONFIRM,
) = range(12)

# Supported chains
SUPPORTED_CHAINS = [
    "Solana", "Ethereum", "Base", "BSC", "Arbitrum", 
    "Polygon", "Avalanche", "Optimism", "Fantom", "Cronos"
]

# Welcome banner image path (place your image here)
WELCOME_BANNER_PATH = "./home.png"


class ErrorType(Enum):
    """Types of errors that can occur during form submission"""
    TOKEN_ALREADY_ENHANCED = "TOKEN_ALREADY_ENHANCED"
    REQUIRES_TAKEOVER_CLAIM = "REQUIRES_TAKEOVER_CLAIM"
    INVALID_ADDRESS = "INVALID_ADDRESS"
    REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
    IMAGE_SIZE_ERROR = "IMAGE_SIZE_ERROR"
    IMAGE_RATIO_ERROR = "IMAGE_RATIO_ERROR"
    IMAGE_FORMAT_ERROR = "IMAGE_FORMAT_ERROR"
    UPLOAD_FAILED = "UPLOAD_FAILED"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


@dataclass
class FormError:
    """Represents an error detected on the form"""
    error_type: ErrorType
    message: str
    field: Optional[str] = None
    suggestion: Optional[str] = None


@dataclass
class UserSession:
    """Store user's order data during conversation"""
    chain: str = ""
    token_address: str = ""
    description: str = ""
    website_url: Optional[str] = None
    x_url: Optional[str] = None
    telegram_url: Optional[str] = None
    icon_image_path: Optional[str] = None
    header_image_path: Optional[str] = None
    resize_image_path: Optional[str] = None
    resize_type: Optional[str] = None  # "icon" or "header"
    form_errors: List[FormError] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain": self.chain,
            "token_address": self.token_address,
            "description": self.description,
            "website_url": self.website_url,
            "x_url": self.x_url,
            "telegram_url": self.telegram_url,
            "icon_image_path": self.icon_image_path,
            "header_image_path": self.header_image_path,
        }
    
    def clear_errors(self):
        self.form_errors = []
    
    def add_error(self, error: FormError):
        self.form_errors.append(error)


class QRScanner:
    """QR Code scanner using zxing-cpp"""
    
    @staticmethod
    def scan(image_path: str) -> list[str]:
        """Scan QR codes from an image file"""
        try:
            image = Image.open(image_path)
            results = zxingcpp.read_barcodes(image)
            return [result.text for result in results]
        except Exception as e:
            logger.error(f"QR scan error: {e}")
            return []


class ImageProcessor:
    """Process and resize images for DexScreener requirements - STRICT MODE"""

    ICON_RATIO = 1.0      # 1:1 for icons (EXACT)
    HEADER_RATIO = 3.0    # 3:1 for headers (EXACT)
    HEADER_WIDTH = 600    # Required header width (EXACT)

    @staticmethod
    def is_valid_icon(width: int, height: int) -> bool:
        """Check if image is EXACTLY 1:1 ratio"""
        return width == height

    @staticmethod
    def is_valid_header(width: int, height: int) -> bool:
        """Check if image is EXACTLY 3:1 ratio with 600px width"""
        return width == 600 and height == 200

    @staticmethod
    def center_crop(image: Image.Image, target_ratio: float) -> Image.Image:
        """Center crop image to target aspect ratio"""
        w, h = image.size
        current_ratio = w / h

        if current_ratio > target_ratio:
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            return image.crop((left, 0, left + new_w, h))
        else:
            new_h = int(w / target_ratio)
            top = (h - new_h) // 2
            return image.crop((0, top, w, top + new_h))

    @staticmethod
    def resize_to_width(image: Image.Image, target_width: int) -> Image.Image:
        """Resize image to target width maintaining aspect ratio"""
        w, h = image.size
        scale = target_width / w
        new_height = int(h * scale)
        return image.resize((target_width, new_height), Image.LANCZOS)

    @classmethod
    def process_icon(cls, image_path: str) -> tuple[str, str]:
        """
        Process icon image to EXACT 1:1 ratio
        Returns (output_path, status_message)
        """
        image = Image.open(image_path)
        width, height = image.size
        original_size = f"{width}x{height}"

        # STRICT CHECK: Must be exactly 1:1
        if cls.is_valid_icon(width, height):
            return image_path, f"✔ Valid 1:1 icon ({original_size})"

        # Auto-crop to exact 1:1
        cropped = cls.center_crop(image, cls.ICON_RATIO)

        # Ensure EXACT square dimensions
        size = min(cropped.size)
        cropped = cropped.resize((size, size), Image.LANCZOS)

        output_path = image_path.rsplit(".", 1)[0] + "_1x1.png"
        cropped.save(output_path, "PNG")

        new_size = f"{cropped.size[0]}x{cropped.size[1]}"
        return output_path, f"📐 Cropped: {original_size} → {new_size}"

    @classmethod
    def process_header(cls, image_path: str) -> tuple[str, str]:
        """
        Process header image to EXACT 3:1 ratio (600x200)
        Returns (output_path, status_message)
        """
        image = Image.open(image_path)
        width, height = image.size
        original_size = f"{width}x{height}"

        # STRICT CHECK: Must be exactly 600x200
        if cls.is_valid_header(width, height):
            return image_path, f"✔ Valid 3:1 header ({original_size})"

        # Auto-crop to 3:1 ratio first
        cropped = cls.center_crop(image, cls.HEADER_RATIO)

        # Resize to EXACT 600x200
        resized = cropped.resize((600, 200), Image.LANCZOS)

        output_path = image_path.rsplit(".", 1)[0] + "_600x200.png"
        resized.save(output_path, "PNG")

        return output_path, f"📐 Resized: {original_size} → 600x200"

    @classmethod
    def validate_icon(cls, image_path: str) -> tuple[bool, str]:
        """Validate if icon meets STRICT requirements"""
        image = Image.open(image_path)
        w, h = image.size
        if w != h:
            return False, f"❌ Invalid: {w}x{h} is not 1:1. Must be square."
        return True, f"✔ Valid: {w}x{h}"

    @classmethod
    def validate_header(cls, image_path: str) -> tuple[bool, str]:
        """Validate if header meets STRICT requirements"""
        image = Image.open(image_path)
        w, h = image.size
        if w != 600 or h != 200:
            return False, f"❌ Invalid: {w}x{h}. Must be exactly 600x200."
        return True, f"✔ Valid: {w}x{h}"


class DexScreenerAutomation:
    """Handles DexScreener form automation with live status updates and error detection"""
    
    # Error detection selectors - comprehensive list for DexScreener
    ERROR_SELECTORS = [
        ".text-destructive",           # Primary error class on DexScreener
        '[class*="text-destructive"]',
        '[class*="destructive"]',
        ".text-red-500",
        ".text-red-400",
        '[class*="text-red"]',
        ".text-danger",
        '[class*="error"]',
        '[role="alert"]',
        ".error-message",
        '[data-error="true"]',
    ]
    
    # Known error patterns and their types
    ERROR_PATTERNS = {
        "already contains enhanced token info": (ErrorType.TOKEN_ALREADY_ENHANCED, "Token already has Enhanced Token Info. Consider a Community Takeover Claim."),
        "community takeover": (ErrorType.REQUIRES_TAKEOVER_CLAIM, "This token requires a Community Takeover Claim."),
        "invalid address": (ErrorType.INVALID_ADDRESS, "The token address is invalid. Please check and try again."),
        "invalid token": (ErrorType.INVALID_ADDRESS, "The token address is invalid. Please check and try again."),
        "required": (ErrorType.REQUIRED_FIELD_MISSING, "This field is required."),
        "invalid width": (ErrorType.IMAGE_SIZE_ERROR, "Image width does not meet requirements."),
        "invalid height": (ErrorType.IMAGE_SIZE_ERROR, "Image height does not meet requirements."),
        "min": (ErrorType.IMAGE_SIZE_ERROR, "Image is too small. Check minimum size requirements."),
        "max": (ErrorType.IMAGE_SIZE_ERROR, "Image is too large. Check maximum size requirements."),
        "ratio": (ErrorType.IMAGE_RATIO_ERROR, "Image aspect ratio is incorrect."),
        "aspect": (ErrorType.IMAGE_RATIO_ERROR, "Image aspect ratio is incorrect."),
        "format": (ErrorType.IMAGE_FORMAT_ERROR, "Image format is not supported. Use PNG or JPG."),
        "upload failed": (ErrorType.UPLOAD_FAILED, "Image upload failed. Please try again."),
        "failed to upload": (ErrorType.UPLOAD_FAILED, "Image upload failed. Please try again."),
        "network": (ErrorType.NETWORK_ERROR, "Network error occurred. Please try again."),
    }
    
    def __init__(self, master_profile_dir: str = "./browser_profile", temp_profiles_dir: str = "./temp_profiles"):
        """
        Initialize automation with shared login support for concurrent users.
        
        - master_profile_dir: Single shared login profile (where /login saves credentials)
        - temp_profiles_dir: Directory for per-user temporary copies during automation
        
        This allows a single DexScreener account to be used by multiple users concurrently.
        """
        self.master_profile_dir = master_profile_dir
        self.temp_profiles_dir = temp_profiles_dir
        os.makedirs(master_profile_dir, exist_ok=True)
        os.makedirs(temp_profiles_dir, exist_ok=True)
        os.makedirs("./screenshots", exist_ok=True)
        os.makedirs("./user_uploads", exist_ok=True)
        os.makedirs("./assets", exist_ok=True)

    async def get_user_profile_dir(self, user_id: int) -> str:
        """
        Get a temporary browser profile directory for a user.
        Copies the master login profile to enable concurrent sessions.
        Uses async file operations to avoid blocking the event loop.
        """
        import shutil
        
        user_profile_dir = os.path.join(self.temp_profiles_dir, f"user_{user_id}")
        
        # Copy master profile to user's temp profile if master exists
        if os.path.exists(self.master_profile_dir) and os.listdir(self.master_profile_dir):
            # Remove old temp profile if exists (run in thread to avoid blocking)
            if os.path.exists(user_profile_dir):
                try:
                    await asyncio.to_thread(shutil.rmtree, user_profile_dir)
                except Exception as e:
                    logger.warning(f"Could not clean old profile for user {user_id}: {e}")
            
            # Copy master profile (run in thread to avoid blocking)
            try:
                await asyncio.to_thread(shutil.copytree, self.master_profile_dir, user_profile_dir, dirs_exist_ok=True)
                logger.info(f"Copied master profile to user {user_id}")
            except Exception as e:
                logger.error(f"Failed to copy master profile for user {user_id}: {e}")
                os.makedirs(user_profile_dir, exist_ok=True)
        else:
            os.makedirs(user_profile_dir, exist_ok=True)
        
        return user_profile_dir
    
    async def cleanup_user_profile(self, user_id: int):
        """Clean up temporary profile after automation completes (async to avoid blocking)."""
        import shutil
        user_profile_dir = os.path.join(self.temp_profiles_dir, f"user_{user_id}")
        try:
            if os.path.exists(user_profile_dir):
                await asyncio.to_thread(shutil.rmtree, user_profile_dir)
                logger.info(f"Cleaned up temp profile for user {user_id}")
        except Exception as e:
            logger.warning(f"Could not cleanup profile for user {user_id}: {e}")

    async def get_error_messages(self, page) -> List[str]:
        """
        Detect and return all red error messages on the page.
        These have the class 'text-destructive' on DexScreener.
        """
        errors = []
        seen_texts = set()  # Avoid duplicates

        for selector in self.ERROR_SELECTORS:
            try:
                error_elements = page.locator(selector)
                count = await error_elements.count()

                for i in range(count):
                    element = error_elements.nth(i)
                    try:
                        if await element.is_visible(timeout=500):
                            text = await element.text_content(timeout=500)
                            if text and text.strip():
                                cleaned_text = text.strip()
                                # Normalize text for deduplication
                                normalized = cleaned_text.lower()
                                if normalized not in seen_texts and len(cleaned_text) > 3:
                                    seen_texts.add(normalized)
                                    errors.append(cleaned_text)
                    except:
                        continue
            except Exception as e:
                logger.debug(f"Error checking selector {selector}: {e}")
                continue

        return errors

    async def check_for_errors(self, page, context: str = "general") -> Dict[str, Any]:
        """
        Check page for errors and return structured error info.
        
        Args:
            page: Playwright page object
            context: Where the check is happening (e.g., "token_address", "icon_upload")
        """
        errors = await self.get_error_messages(page)

        result = {
            "has_errors": len(errors) > 0,
            "errors": errors,
            "error_types": [],
            "parsed_errors": [],
            "context": context,
        }

        # Categorize and parse errors
        for error in errors:
            error_lower = error.lower()
            error_parsed = False
            
            for pattern, (error_type, suggestion) in self.ERROR_PATTERNS.items():
                if pattern in error_lower:
                    form_error = FormError(
                        error_type=error_type,
                        message=error,
                        field=context,
                        suggestion=suggestion
                    )
                    result["parsed_errors"].append(form_error)
                    if error_type.value not in result["error_types"]:
                        result["error_types"].append(error_type.value)
                    error_parsed = True
                    break
            
            # If no pattern matched, add as unknown error
            if not error_parsed:
                form_error = FormError(
                    error_type=ErrorType.UNKNOWN_ERROR,
                    message=error,
                    field=context,
                    suggestion="Please review this error and try again."
                )
                result["parsed_errors"].append(form_error)
                if ErrorType.UNKNOWN_ERROR.value not in result["error_types"]:
                    result["error_types"].append(ErrorType.UNKNOWN_ERROR.value)

        return result

    async def wait_and_check_errors(self, page, wait_time: int = 2000, context: str = "general") -> Dict[str, Any]:
        """Wait for potential errors to appear then check for them"""
        await page.wait_for_timeout(wait_time)
        return await self.check_for_errors(page, context)

    async def is_logged_in(self, page) -> bool:
        """Check if user is logged in"""
        try:
            await page.wait_for_selector("text=Sign Out", timeout=3000)
            return True
        except:
            return False

    async def launch_browser(self, p, user_id: int):
        """Launch persistent browser context with user-specific profile for concurrent support."""
        import json
        user_profile_dir = await self.get_user_profile_dir(user_id)
        context = await p.chromium.launch_persistent_context(
            user_data_dir=user_profile_dir,
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            ignore_default_args=["--enable-automation"],
        )

        # Inject portable session cookies if available (cross-platform, from export_session.py)
        session_file = "./session.json"
        if os.path.exists(session_file):
            try:
                with open(session_file) as f:
                    storage = json.load(f)
                cookies = storage.get("cookies", [])
                if cookies:
                    await context.add_cookies(cookies)
                    logger.info(f"Injected {len(cookies)} cookies from session.json")
            except Exception as e:
                logger.warning(f"Could not load session.json: {e}")

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://marketplace.dexscreener.com/product/token-info/order")

        if not await self.is_logged_in(page):
            raise Exception("Not logged in. Please run /login first to setup browser session.")

        return page, context

    async def select_chain(self, page, chain_name: str, status_callback) -> Dict[str, Any]:
        """Select blockchain from dropdown and check for errors"""
        await status_callback(f"🔗 Selecting chain...")
        
        await page.click('button:has-text("Select a chain")')
        await page.wait_for_timeout(1000)

        try:
            await page.click(f'text="{chain_name}"', timeout=5000)
        except:
            await page.click(f'[role="option"]:has-text("{chain_name}")')

        await status_callback(f"{chain_name} selected")
        
        # Check for errors after chain selection
        error_result = await self.wait_and_check_errors(page, 1500, "chain_selection")
        return error_result

    async def fill_token_address(self, page, token_address: str, status_callback) -> Dict[str, Any]:
        """Fill token address field and check for errors comprehensively"""
        await status_callback(f"📍 Filling token address...")
        
        # Clear any existing value first
        await page.fill('input[name="tokenIdentity.tokenAddress"]', "")
        await page.wait_for_timeout(500)
        
        # Fill the token address
        await page.fill('input[name="tokenIdentity.tokenAddress"]', token_address)
        
        # Wait longer for DexScreener to validate the token (it makes API calls)
        await status_callback(f"🔍 Validating token...")
        await page.wait_for_timeout(3000)
        
        # Check for any errors
        error_result = await self.check_for_errors(page, "token_address")
        
        # Additional specific checks for token-related errors
        if error_result["has_errors"]:
            for error in error_result["errors"]:
                error_lower = error.lower()
                if "already contains enhanced token info" in error_lower:
                    error_result["token_already_paid"] = True
                    await status_callback(f"⚠️ Token already has Enhanced Info!")
                elif "community takeover" in error_lower:
                    error_result["requires_takeover"] = True
                    await status_callback(f"⚠️ Requires Community Takeover Claim")
        
        return error_result

    async def fill_description(self, page, description: str, status_callback) -> Dict[str, Any]:
        """Fill description field and check for errors"""
        await status_callback(f"📝 Filling description...")
        await page.fill('textarea[name="description"]', description)
        
        error_result = await self.wait_and_check_errors(page, 1000, "description")
        return error_result

    async def add_social_link(self, page, social_type: str, url: str, status_callback) -> Dict[str, Any]:
        """Add a social media link and check for errors"""
        await status_callback(f"🔗 Adding {social_type}...")
        
        result = {"success": False, "errors": []}
        
        try:
            # Click on the "Add {social_type}" area
            add_selector = f'div.hover\\:cursor-pointer:has-text("Add {social_type}")'
            
            try:
                await page.click(add_selector, timeout=3000)
            except:
                add_btn = page.locator(f'text="Add {social_type}"').first
                await add_btn.click()
            
            await page.wait_for_timeout(1500)
            
            # Find and fill the URL input
            url_input = page.locator('input[type="text"], input[type="url"]').last
            await url_input.fill(url)
            await page.wait_for_timeout(500)
            
            # Try clicking save/add button
            for sel in ['button:has-text("Save")', 'button:has-text("Add")', 'button:has-text("Confirm")']:
                try:
                    btn = page.locator(sel).last
                    if await btn.is_visible():
                        await btn.click(timeout=1000)
                        break
                except:
                    continue
            else:
                await page.keyboard.press("Enter")
            
            await page.wait_for_timeout(1000)
            
            # Check for errors after adding social link
            error_result = await self.check_for_errors(page, f"social_{social_type.lower()}")
            
            if error_result["has_errors"]:
                result["errors"] = error_result["errors"]
                await status_callback(f"⚠️ Error adding {social_type}: {error_result['errors'][0][:50]}")
            else:
                result["success"] = True
                await status_callback(f"{social_type} added")
            
        except Exception as e:
            logger.error(f"Error adding {social_type}: {e}")
            result["errors"].append(str(e))
            await status_callback(f"⚠️ Failed to add {social_type}")
        
        return result

    async def upload_image_robust(self, page, image_path: str, image_type: str, status_callback) -> Dict[str, Any]:
        """
        Upload image and check for errors comprehensively.
        Returns detailed error info including specific DexScreener error messages.
        """
        await status_callback(f"📷 Uploading {image_type}...")
        
        result = {
            "success": False,
            "errors": [],
            "error_types": [],
            "raw_errors": [],
        }
        
        if not os.path.exists(image_path):
            result["errors"].append("File not found")
            result["error_types"].append(ErrorType.UPLOAD_FAILED.value)
            return result
        
        try:
            file_inputs = page.locator('input[type="file"]')
            count = await file_inputs.count()
            
            if count == 0:
                result["errors"].append("No file input found on page")
                result["error_types"].append(ErrorType.UPLOAD_FAILED.value)
                return result
            
            # Icon = index 0, Header = index 1
            target_index = 0 if image_type.lower() == "icon" else min(1, count - 1)
            
            file_input = file_inputs.nth(target_index)
            
            # Make input interactable
            await file_input.evaluate("""el => {
                el.style.display = 'block';
                el.style.visibility = 'visible';  
                el.style.opacity = '1';
                el.style.position = 'relative';
                el.style.zIndex = '9999';
            }""")
            await page.wait_for_timeout(500)
            
            # Get errors before upload for comparison
            errors_before = await self.get_error_messages(page)
            
            # Upload the file
            await file_input.set_input_files(image_path)
            await status_callback(f"⏳ Processing {image_type}...")
            
            # Wait for upload processing and potential error messages
            await page.wait_for_timeout(3000)
            
            # Check for new errors
            error_result = await self.check_for_errors(page, f"{image_type.lower()}_upload")
            
            # Find new errors (not present before upload)
            errors_before_lower = [e.lower() for e in errors_before]
            new_errors = []
            for error in error_result["errors"]:
                if error.lower() not in errors_before_lower:
                    new_errors.append(error)
            
            # Also check for errors near the upload area specifically
            upload_area_errors = await self._check_upload_area_errors(page, image_type)
            for err in upload_area_errors:
                if err not in new_errors:
                    new_errors.append(err)
            
            if new_errors:
                result["errors"] = new_errors
                result["raw_errors"] = new_errors
                result["error_types"] = error_result["error_types"]
                
                # Log and report errors
                error_summary = "; ".join(new_errors[:3])  # First 3 errors
                await status_callback(f"❌ {image_type} error: {error_summary[:80]}")
                logger.warning(f"Image upload errors for {image_type}: {new_errors}")
            else:
                result["success"] = True
                await status_callback(f"{image_type} uploaded")
            
            return result
            
        except Exception as e:
            logger.error(f"Image upload error: {e}")
            result["errors"].append(str(e)[:100])
            result["error_types"].append(ErrorType.UPLOAD_FAILED.value)
            await status_callback(f"❌ {image_type} upload failed: {str(e)[:50]}")
            return result

    async def _check_upload_area_errors(self, page, image_type: str) -> List[str]:
        """Check for errors specifically near the image upload area"""
        errors = []
        
        # Selectors that might contain upload-specific errors
        upload_error_selectors = [
            f'div:has-text("{image_type}") .text-destructive',
            f'div:has-text("{image_type}") [class*="error"]',
            f'div:has-text("{image_type}") .text-red-500',
            # Look for error near file input
            'input[type="file"] ~ .text-destructive',
            'input[type="file"] ~ [class*="error"]',
            # Common upload error containers
            '.upload-error',
            '.file-error',
            '[data-upload-error]',
        ]
        
        for selector in upload_error_selectors:
            try:
                elements = page.locator(selector)
                count = await elements.count()
                for i in range(count):
                    el = elements.nth(i)
                    if await el.is_visible(timeout=500):
                        text = await el.text_content(timeout=500)
                        if text and text.strip() and len(text.strip()) > 3:
                            errors.append(text.strip())
            except:
                continue
        
        return errors

    async def accept_terms(self, page, status_callback) -> Dict[str, Any]:
        """Check terms checkboxes and verify"""
        await status_callback("☑️ Accepting terms...")
        
        result = {"success": False, "errors": []}
        
        try:
            # Method 1: Click on label text
            labels = [
                'label:has-text("I understand that all supplied")',
                'label:has-text("I understand and accept")',
            ]
            
            for label_sel in labels:
                try:
                    label = page.locator(label_sel).first
                    if await label.is_visible():
                        await label.click()
                        await page.wait_for_timeout(300)
                except:
                    pass
            
            # Method 2: Direct checkbox click with force
            checkboxes = page.locator('input[type="checkbox"]')
            count = await checkboxes.count()
            
            for i in range(count):
                try:
                    cb = checkboxes.nth(i)
                    if not await cb.is_checked():
                        await cb.evaluate("el => el.click()")
                        await page.wait_for_timeout(200)
                except:
                    pass
            
            # Method 3: JavaScript force check
            await page.evaluate("""
                document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                    if (!cb.checked) {
                        cb.checked = true;
                        cb.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                });
            """)
            
            await page.wait_for_timeout(500)
            
            # Check for errors after accepting terms
            error_result = await self.check_for_errors(page, "terms")
            
            if error_result["has_errors"]:
                result["errors"] = error_result["errors"]
            else:
                result["success"] = True
                await status_callback("✅ Terms accepted")
            
        except Exception as e:
            result["errors"].append(str(e))
            await status_callback(f"⚠️ Terms: {str(e)[:30]}")
        
        return result

    async def click_order_button(self, page, status_callback) -> Dict[str, Any]:
        """Click the order button and check for errors"""
        await status_callback("🛒 Submitting order...")
        
        result = {"success": False, "errors": []}
        order_clicked = False
        
        # Method 1: Direct button selector
        order_selectors = [
            'button:has-text("Order Now")',
            'button:has-text("Order")',
            'button[type="submit"]',
            'button.order-btn',
            'button:has-text("Submit")',
        ]
        
        for sel in order_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    await btn.click()
                    order_clicked = True
                    break
            except:
                continue
        
        # Method 2: JavaScript click
        if not order_clicked:
            try:
                await page.evaluate("""
                    const btns = document.querySelectorAll('button');
                    for (const btn of btns) {
                        if (btn.textContent.includes('Order')) {
                            btn.click();
                            break;
                        }
                    }
                """)
                order_clicked = True
            except:
                pass
        
        # Method 3: Press Enter on form
        if not order_clicked:
            try:
                await page.keyboard.press("Enter")
            except:
                pass
        
        # Wait for response and check for errors
        await page.wait_for_timeout(3000)
        
        error_result = await self.check_for_errors(page, "order_submission")
        
        if error_result["has_errors"]:
            result["errors"] = error_result["errors"]
            await status_callback(f"⚠️ Order error: {error_result['errors'][0][:50]}")
        else:
            result["success"] = True
        
        return result

    async def click_moonpay_button(self, page, status_callback) -> Dict[str, Any]:
        """Click the MoonPay payment button and wait for QR page"""
        await status_callback("💳 Selecting MoonPay...")
        
        result = {"success": False, "errors": []}
        
        # Selectors for MoonPay button
        moonpay_selectors = [
            'button:has-text("Pay with crypto or credit card (MoonPay)")',
            'button:has-text("MoonPay")',
            'a:has-text("Pay with crypto or credit card (MoonPay)")',
            'a:has-text("MoonPay")',
            '[class*="button"]:has-text("MoonPay")',
        ]
        
        clicked = False
        for selector in moonpay_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    clicked = True
                    await status_callback("MoonPay selected")
                    break
            except:
                continue
        
        if not clicked:
            # Try JavaScript click as fallback
            try:
                await page.evaluate("""
                    const buttons = document.querySelectorAll('button, a');
                    for (const btn of buttons) {
                        if (btn.textContent.includes('MoonPay')) {
                            btn.click();
                            break;
                        }
                    }
                """)
                clicked = True
            except Exception as e:
                result["errors"].append(f"Could not click MoonPay button: {str(e)[:50]}")
        
        if clicked:
            # Wait for MoonPay page to load
            await status_callback("⏳ Loading MoonPay...")
            await page.wait_for_timeout(6000)  # Increased from 4000 for page load
            
            # Now click "Pay with QR" button
            await status_callback("📱 Selecting QR payment...")
            qr_clicked = await self.click_pay_with_qr(page)
            
            if qr_clicked:
                await status_callback("QR payment selected")
                await page.wait_for_timeout(5000)  # Increased from 3000 for QR page load
            else:
                await status_callback("⚠️ Could not find QR option, using current page")
            
            result["success"] = True
        
        return result

    async def click_pay_with_qr(self, page) -> bool:
        """Click the 'Pay with QR' button on MoonPay page"""
        
        # Selectors for "Pay with QR" button
        qr_selectors = [
            'button:has-text("Pay with QR")',
            'a:has-text("Pay with QR")',
            '[data-testid="qr-payment"]',
            'button:has-text("QR")',
            'a:has-text("QR")',
            'div:has-text("Pay with QR")',
            '[class*="qr"]',
            # MoonPay specific selectors
            'button:has-text("Show QR")',
            'button:has-text("QR Code")',
            'a:has-text("QR Code")',
        ]
        
        for selector in qr_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    return True
            except:
                continue
        
        # Try JavaScript click as fallback
        try:
            result = await page.evaluate("""
                const elements = document.querySelectorAll('button, a, div');
                for (const el of elements) {
                    const text = el.textContent.toLowerCase();
                    if (text.includes('pay with qr') || text.includes('qr code') || text.includes('show qr')) {
                        el.click();
                        return true;
                    }
                }
                return false;
            """)
            return result
        except:
            return False

    async def submit_order(
        self, 
        session: UserSession, 
        status_callback: Callable[[str], Any],
        user_id: int
    ) -> Dict[str, Any]:
        """
        Submit DexScreener order and capture payment QR code
        With comprehensive error detection at each step.
        Each user gets their own browser profile for concurrent automation support.
        """
        result = {
            "success": False,
            "message": "",
            "payment_url": None,
            "order_number": None,
            "payment_page_screenshot": None,
            "qr_page_screenshot": None,
            "screenshot_path": None,  # Keep for backward compatibility
            "token_already_paid": False,
            "requires_takeover": False,
            "image_errors": [],
            "form_errors": [],
            "all_errors": [],
        }

        try:
            await status_callback("Starting automation...")
            
            async with async_playwright() as p:
                page, context = await self.launch_browser(p, user_id)
                await status_callback("✅ Browser ready")

                # 1. Select Chain
                chain_result = await self.select_chain(page, session.chain, status_callback)
                if chain_result["has_errors"]:
                    result["form_errors"].extend(chain_result["errors"])

                # 2. Fill Token Address and check for errors
                token_result = await self.fill_token_address(page, session.token_address, status_callback)
                
                if token_result.get("token_already_paid"):
                    result["token_already_paid"] = True
                    result["message"] = "Token already has Enhanced Token Info. Consider a Community Takeover Claim."
                    result["all_errors"] = token_result.get("errors", [])
                    await context.close()
                    return result
                
                if token_result.get("requires_takeover"):
                    result["requires_takeover"] = True
                    result["message"] = "This token requires a Community Takeover Claim."
                    result["all_errors"] = token_result.get("errors", [])
                    await context.close()
                    return result
                
                if token_result["has_errors"]:
                    # Check if it's a critical error that should stop the process
                    critical_errors = [ErrorType.INVALID_ADDRESS.value, ErrorType.TOKEN_ALREADY_ENHANCED.value]
                    if any(et in token_result.get("error_types", []) for et in critical_errors):
                        result["message"] = "Token address error: " + "; ".join(token_result["errors"][:2])
                        result["form_errors"] = token_result["errors"]
                        result["all_errors"] = token_result["errors"]
                        await context.close()
                        return result

                # 3. Fill Description
                desc_result = await self.fill_description(page, session.description, status_callback)
                if desc_result["has_errors"]:
                    result["form_errors"].extend(desc_result["errors"])

                # 4. Add Social Links
                if session.website_url:
                    web_result = await self.add_social_link(page, "Website", session.website_url, status_callback)
                    if web_result.get("errors"):
                        result["form_errors"].extend(web_result["errors"])
                        
                if session.x_url:
                    x_result = await self.add_social_link(page, "X", session.x_url, status_callback)
                    if x_result.get("errors"):
                        result["form_errors"].extend(x_result["errors"])
                        
                if session.telegram_url:
                    tg_result = await self.add_social_link(page, "Telegram", session.telegram_url, status_callback)
                    if tg_result.get("errors"):
                        result["form_errors"].extend(tg_result["errors"])

                # 5. Upload Images with detailed error checking
                if session.icon_image_path:
                    icon_result = await self.upload_image_robust(page, session.icon_image_path, "Icon", status_callback)
                    if not icon_result["success"]:
                        for err in icon_result["errors"]:
                            result["image_errors"].append(f"Icon: {err}")
                        await status_callback(f"⚠️ Icon rejected: {icon_result['errors'][0][:50] if icon_result['errors'] else 'Unknown error'}")
                
                if session.header_image_path:
                    header_result = await self.upload_image_robust(page, session.header_image_path, "Header", status_callback)
                    if not header_result["success"]:
                        for err in header_result["errors"]:
                            result["image_errors"].append(f"Header: {err}")
                        await status_callback(f"⚠️ Header rejected: {header_result['errors'][0][:50] if header_result['errors'] else 'Unknown error'}")

                # If there are image errors, abort
                if result["image_errors"]:
                    result["message"] = "Image upload rejected by DexScreener"
                    result["all_errors"] = result["image_errors"]
                    await context.close()
                    return result

                # 6. Accept Terms
                terms_result = await self.accept_terms(page, status_callback)
                if terms_result.get("errors"):
                    result["form_errors"].extend(terms_result["errors"])

                # 7. Final error check before submitting
                final_check = await self.check_for_errors(page, "pre_submit")
                if final_check["has_errors"]:
                    # Check if any are critical
                    for error in final_check["errors"]:
                        if "enhanced token info" in error.lower() or "already" in error.lower():
                            result["token_already_paid"] = True
                            result["message"] = error
                            result["all_errors"] = [error]
                            await context.close()
                            return result

                # 8. Click Order Now
                order_result = await self.click_order_button(page, status_callback)
                
                if order_result.get("errors"):
                    result["form_errors"].extend(order_result["errors"])
                    # If order failed due to errors, return
                    if not order_result["success"]:
                        result["message"] = "Order submission failed: " + "; ".join(order_result["errors"][:2])
                        result["all_errors"] = order_result["errors"]
                        await context.close()
                        return result

                await status_callback("⏳ Loading payment page...")
                await page.wait_for_timeout(6000)

                # 9. Check for errors on payment page
                payment_errors = await self.check_for_errors(page, "payment_page")
                if payment_errors["has_errors"]:
                    result["form_errors"].extend(payment_errors["errors"])

                # 10. Extract order number from page
                try:
                    order_text = await page.locator('text=#').first.text_content(timeout=3000)
                    if order_text and '#' in order_text:
                        # Extract order number like #1767466501030
                        match = re.search(r'#(\d+)', order_text)
                        if match:
                            result["order_number"] = match.group(1)
                            await status_callback(f"📋 Order #{result['order_number']}")
                except:
                    pass

                # 11. Take screenshot of payment options page
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                payment_page_path = f"./screenshots/payment_options_{session.token_address[:8]}_{timestamp}.png"
                await page.screenshot(path=payment_page_path, full_page=True)
                result["payment_page_screenshot"] = payment_page_path
                await status_callback("📸 Payment page captured")

                # 12. Click MoonPay button
                moonpay_result = await self.click_moonpay_button(page, status_callback)
                
                if not moonpay_result["success"]:
                    # If MoonPay click failed, still return success with payment page screenshot
                    result["success"] = True
                    result["screenshot_path"] = payment_page_path
                    result["message"] = "Order submitted! Could not auto-select MoonPay. Please use the payment page screenshot."
                    result["all_errors"] = moonpay_result.get("errors", [])
                    await context.close()
                    return result

                # 13. Wait for QR code page and take screenshot
                await page.wait_for_timeout(5000)  # Increased from 3000 for QR page to fully load
                
                qr_page_path = f"./screenshots/qr_code_{session.token_address[:8]}_{timestamp}.png"
                await page.screenshot(path=qr_page_path, full_page=True)
                result["qr_page_screenshot"] = qr_page_path
                result["screenshot_path"] = qr_page_path  # Primary screenshot is QR page
                await status_callback("📸 QR code page captured")

                # 14. Scan QR code from the QR page screenshot
                await page.wait_for_timeout(2000)  # Extra wait for QR to be fully rendered
                await status_callback("🔍 Scanning QR code...")
                qr_results = QRScanner.scan(qr_page_path)
                
                if qr_results:
                    for url in qr_results:
                        if any(x in url.lower() for x in ['pay', 'moonpay', 'hel.io', 'crypto', 'checkout']):
                            result["payment_url"] = url
                            await status_callback("Payment link found!")
                            break
                    if not result["payment_url"]:
                        result["payment_url"] = qr_results[0]
                        await status_callback("QR code scanned!")
                else:
                    await status_callback("⚠️ No QR code found, check screenshot")

                result["success"] = True
                result["message"] = "Order submitted successfully!"
                result["all_errors"] = result["form_errors"] + result["image_errors"]

                await status_callback("✅ Done!")
                await context.close()

        except Exception as e:
            result["message"] = str(e)
            result["all_errors"].append(str(e))
            await status_callback(f"❌ Error: {str(e)[:50]}")
            logger.error(f"Automation error: {e}", exc_info=True)

        return result


class DexScreenerBot:
    """Telegram bot for DexScreener automation"""

    def __init__(self, token: str):
        self.token = token
        self.automation = DexScreenerAutomation()
        self.user_sessions: Dict[int, UserSession] = {}
        self.active_queue: Dict[int, dict] = {}  # Track active automations for queue display
        self.total_dexes_processed = 0  # Track total successful dex submissions

    def validate_website_url(self, url: str) -> tuple[bool, str]:
        """Validate website URL - must have https:// and should not contain x.com, telegram, or discord links"""
        url_lower = url.lower()
        # Check for https:// prefix first
        if not url.startswith('https://') and not url.startswith('http://'):
            return False, "Please enter the full website URL including https:// (e.g., https://yoursite.com)"
        # Check for social media links that shouldn't be in website field
        if 'x.com' in url_lower or 'twitter.com' in url_lower:
            return False, "Website should not contain X/Twitter links. Please enter your actual website URL."
        if 't.me' in url_lower or 'telegram' in url_lower:
            return False, "Website should not contain Telegram links. Please enter your actual website URL."
        if 'discord.gg' in url_lower or 'discord.com' in url_lower:
            return False, "Website should not contain Discord links. Please enter your actual website URL."
        return True, ""

    def validate_social_url(self, url: str, social_type: str) -> tuple[bool, str]:
        """Validate social URLs - must include https://"""
        if not url.startswith('https://') and not url.startswith('http://'):
            return False, f"❌ Please enter the full {social_type} URL including https:// (e.g., https://x.com/username)"
        return True, ""

    def get_session(self, user_id: int) -> UserSession:
        """Get or create user session"""
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = UserSession()
        return self.user_sessions[user_id]

    async def send_status(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message: str):
        """Send status update to user"""
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            logger.error(f"Failed to send status: {e}")

    def format_errors_for_user(self, errors: List[str], title: str = "Errors") -> str:
        """Format error list for user-friendly display"""
        if not errors:
            return ""

        formatted = f"**{title}:**\n"
        for i, error in enumerate(errors[:5], 1):  # Limit to 5 errors
            # Truncate long error messages
            error_text = error[:100] + "..." if len(error) > 100 else error
            formatted += f"• {error_text}\n"

        if len(errors) > 5:
            formatted += f"_...and {len(errors) - 5} more_\n"

        return formatted

    # ==================== START & MAIN MENU ====================

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Start command - show welcome banner and main menu"""
        user = update.effective_user
        self.user_sessions[user.id] = UserSession()

        keyboard = [
            [InlineKeyboardButton("Pay for Dex", callback_data="pay_dex")],
            [InlineKeyboardButton("🖼️ Resize Image For Dex ", callback_data="resize_only")],
            [InlineKeyboardButton("❓ Help", callback_data="show_help")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        welcome_text = (
            f"Welcome {user.first_name}!\n\n"
            "🤖 **DexPay Automation Bot**\n\n"
            "I can help you:\n\n"
            "• Automate/Pay Dex for your token\n\n"
            "• Resize images for DexScreener (1:1 icon or 3:1 header)\n\n"
            "What would you like to do?"
        )

        # Try to send welcome banner if it exists
        if os.path.exists(WELCOME_BANNER_PATH):
            with open(WELCOME_BANNER_PATH, "rb") as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption=welcome_text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text(
                welcome_text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )

        return MAIN_MENU

    # ==================== RESIZE IMAGE ONLY ====================

    async def resize_only(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Start resize-only flow"""
        query = update.callback_query
        await query.answer()

        keyboard = [
            [
                InlineKeyboardButton("📷 Icon (1:1)", callback_data="resize_icon"),
                InlineKeyboardButton("🖼️ Header (3:1)", callback_data="resize_header"),
            ],
            [InlineKeyboardButton("« Back", callback_data="back_to_main")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        text = (
            "🖼️ **Image Resize Tool**\n\n"
            "What type of image do you want to resize?\n\n"
            "• **Icon (1:1)** - Square format for token icon\n"
            "• **Header (3:1)** - Banner format, 600px width"
        )

        try:
            if query.message.photo:
                await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

        return RESIZE_TYPE

    async def resize_icon_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """User selected icon resize"""
        query = update.callback_query
        await query.answer()

        session = self.get_session(query.from_user.id)
        session.resize_type = "icon"

        await query.message.reply_text(
            "📷 **Icon Resize (1:1)**\n\n"
            "Send me the image you want to resize to square format.",
            parse_mode="Markdown"
        )

        return RESIZE_IMAGE

    async def resize_header_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """User selected header resize"""
        query = update.callback_query
        await query.answer()

        session = self.get_session(query.from_user.id)
        session.resize_type = "header"

        await query.message.reply_text(
            "🖼️ **Header Resize (3:1)**\n\n"
            "Send me the image you want to resize to banner format (600px width).",
            parse_mode="Markdown"
        )

        return RESIZE_IMAGE

    async def process_resize_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Process image for resize-only mode - accepts both photos and file uploads"""
        session = self.get_session(update.effective_user.id)

        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        
        # Handle photo sent as photo
        if update.message.photo:
            photo = update.message.photo[-1]
            file = await photo.get_file()
            file_path = f"./user_uploads/resize_{update.effective_user.id}_{timestamp}.jpg"
            await file.download_to_drive(file_path)
        # Handle image sent as document/file
        elif update.message.document:
            doc = update.message.document
            mime = doc.mime_type or ""
            if mime.startswith("image/") or doc.file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                file = await doc.get_file()
                ext = os.path.splitext(doc.file_name)[1] if doc.file_name else '.jpg'
                file_path = f"./user_uploads/resize_{update.effective_user.id}_{timestamp}{ext}"
                await file.download_to_drive(file_path)
            else:
                await update.message.reply_text("Please send an image file (JPEG, PNG, or WebP).")
                return RESIZE_IMAGE
        else:
            await update.message.reply_text("Please send an image (as photo or file).")
            return RESIZE_IMAGE

        await update.message.reply_text("🔄 Processing image...")

        try:
            if session.resize_type == "icon":
                processed_path, status_msg = ImageProcessor.process_icon(os.path.abspath(file_path))
            else:
                processed_path, status_msg = ImageProcessor.process_header(os.path.abspath(file_path))

            # Get dimensions
            with Image.open(processed_path) as img:
                w, h = img.size

            keyboard = [
                [InlineKeyboardButton("🔄 Resize Another", callback_data="resize_only")],
                [InlineKeyboardButton("Pay for Dex", callback_data="pay_dex")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            with open(processed_path, "rb") as img_file:
                await update.message.reply_photo(
                    photo=img_file,
                    caption=(
                        f"**Image Processed!**\n\n"
                        f"{status_msg}\n"
                        f"Final size: {w}x{h}px\n\n"
                        "What would you like to do next?"
                    ),
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )

        except Exception as e:
            await update.message.reply_text(f"❌ Error processing image: {e}")

        return MAIN_MENU

    # ==================== PAY DEX FLOW ====================

    async def pay_dex(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Start pay DexScreener flow"""
        query = update.callback_query
        await query.answer()

        self.user_sessions[query.from_user.id] = UserSession()

        # Create chain selection keyboard
        keyboard = []
        row = []
        for i, chain in enumerate(SUPPORTED_CHAINS):
            row.append(InlineKeyboardButton(chain, callback_data=f"chain_{chain}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        keyboard.append([InlineKeyboardButton("« Back", callback_data="back_to_main")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        text = (
            "**DexScreener Token Listing**\n\n"
            "📋 **Step 1/6: Select Blockchain**\n\n"
            "Choose your token's blockchain:"
        )

        try:
            if query.message.photo:
                await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

        return CHAIN

    async def back_to_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Go back to main menu"""
        query = update.callback_query
        await query.answer()

        keyboard = [
            [InlineKeyboardButton("Pay for DexScreener Listing", callback_data="pay_dex")],
            [InlineKeyboardButton("🖼️ Resize Image Only", callback_data="resize_only")],
            [InlineKeyboardButton("❓ Help", callback_data="show_help")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        text = (
            "🤖 **DexScreener Automation Bot**\n\n"
            "What would you like to do?"
        )

        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

        return MAIN_MENU

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Show help"""
        query = update.callback_query
        await query.answer()

        keyboard = [
            [InlineKeyboardButton("Start Order", callback_data="pay_dex")],
            [InlineKeyboardButton("« Back", callback_data="back_to_main")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        text = (
            "❓ **Help**\n\n"
            "** Pay for DexScreener Listing:**\n"
            "Automates the entire DexScreener Enhanced Token Info order process:\n"
            "1️⃣ Select blockchain\n"
            "2️⃣ Enter token address\n"
            "3️⃣ Add description\n"
            "4️⃣ Add social links\n"
            "5️⃣ Upload images\n"
            "6️⃣ Submit & get payment link\n\n"
            "**🖼️ Resize Image Only:**\n"
            "Resize images to DexScreener requirements:\n"
            "• Icon: 1:1 square ratio\n"
            "• Header: 3:1 ratio, 600px width\n\n"
            "**Commands:**\n"
            "/start - Main menu\n"
            "/cancel - Cancel operation\n"
            "/login - Setup browser login"
        )

        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

        return MAIN_MENU

    # ==================== CHAIN SELECTION ====================

    async def chain_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle chain selection"""
        query = update.callback_query
        await query.answer()

        chain = query.data.replace("chain_", "")
        session = self.get_session(query.from_user.id)
        session.chain = chain

        await query.edit_message_text(
            f"Chain: **{chain}**\n\n"
            "📋 **Step 2/6: Token Address**\n\n"
            "Enter your token contract address:",
            parse_mode="Markdown"
        )

        return TOKEN_ADDRESS

    # ==================== TOKEN ADDRESS ====================

    async def token_address_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle token address input"""
        session = self.get_session(update.effective_user.id)
        session.token_address = update.message.text.strip()

        await update.message.reply_text(
            f"Token: `{session.token_address[:20]}...`\n\n"
            "📋 **Step 3/6: Description**\n\n"
            "Enter a description for your token:",
            parse_mode="Markdown"
        )

        return DESCRIPTION

    # ==================== DESCRIPTION ====================

    async def description_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle description input"""
        session = self.get_session(update.effective_user.id)
        session.description = update.message.text.strip()

        keyboard = [
            [
                InlineKeyboardButton("🌐 Add Socials", callback_data="add_website"),
                InlineKeyboardButton("⏭️ Skip All", callback_data="skip_socials"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Description saved!\n\n"
            "📋 **Step 4/6: Social Links**\n\n"
            "Add your social media links (optional):",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

        return WEBSITE

    # ==================== SOCIAL LINKS ====================

    async def add_website(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Prompt for website URL"""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🌐 **Website URL**\n\nEnter your website URL (or send 'skip'):",
            parse_mode="Markdown"
        )
        return WEBSITE

    async def website_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle website URL or edited social URL"""
        text = update.message.text.strip()
        session = self.get_session(update.effective_user.id)

        # Check if we're in single social editing mode
        editing_social = context.user_data.get("editing_social")

        if editing_social:
            # Single social edit mode
            if text.lower() == "skip":
                if editing_social == "website":
                    session.website_url = None
                    await update.message.reply_text("Website removed!")
                elif editing_social == "x":
                    session.x_url = None
                    await update.message.reply_text("X URL removed!")
                elif editing_social == "telegram":
                    session.telegram_url = None
                    await update.message.reply_text("Telegram URL removed!")
            else:
                # Validate URL based on type
                if editing_social == "website":
                    is_valid, error_msg = self.validate_website_url(text)
                    if not is_valid:
                        await update.message.reply_text(error_msg)
                        return WEBSITE
                    session.website_url = text
                    await update.message.reply_text("✔ Website updated!")
                elif editing_social == "x":
                    is_valid, error_msg = self.validate_social_url(text, "X")
                    if not is_valid:
                        await update.message.reply_text(error_msg)
                        return WEBSITE
                    session.x_url = text
                    await update.message.reply_text("✔  X URL updated!")
                elif editing_social == "telegram":
                    is_valid, error_msg = self.validate_social_url(text, "Telegram")
                    if not is_valid:
                        await update.message.reply_text(error_msg)
                        return WEBSITE
                    session.telegram_url = text
                    await update.message.reply_text("✔ Telegram URL updated!")

            # Clear editing mode and go back to confirmation
            context.user_data["editing_social"] = None
            return await self.show_confirmation(update, context)

        # Normal flow - entering website for first time
        if text.lower() != "skip":
            # Validate website URL
            is_valid, error_msg = self.validate_website_url(text)
            if not is_valid:
                await update.message.reply_text(error_msg)
                return WEBSITE
            session.website_url = text
            await update.message.reply_text("✔ Website saved!")

        await update.message.reply_text(
            "🐦 **X (Twitter) URL**\n\nEnter your X URL (or send 'skip'):",
            parse_mode="Markdown"
        )
        return X_URL

    async def x_url_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle X URL"""
        text = update.message.text.strip()
        session = self.get_session(update.effective_user.id)

        if text.lower() != "skip":
            # Validate X URL has https://
            is_valid, error_msg = self.validate_social_url(text, "X")
            if not is_valid:
                await update.message.reply_text(error_msg)
                return X_URL
            session.x_url = text
            await update.message.reply_text("✔ X URL saved!")

        await update.message.reply_text(
            "📱 **Telegram URL**\n\nEnter your Telegram URL (or send 'skip'):",
            parse_mode="Markdown"
        )
        return TELEGRAM_URL

    async def telegram_url_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle Telegram URL"""
        text = update.message.text.strip()
        session = self.get_session(update.effective_user.id)

        if text.lower() != "skip":
            # Validate Telegram URL has https://
            is_valid, error_msg = self.validate_social_url(text, "Telegram")
            if not is_valid:
                await update.message.reply_text(error_msg)
                return TELEGRAM_URL
            session.telegram_url = text
            await update.message.reply_text("✔ Telegram URL saved!")

        # Go directly to icon upload - no skip option
        await update.message.reply_text(
            "📋 **Step 5/6: Images**\n\n"
            "📷 **Icon Image (Required)**\n\n"
            "Send your icon image.\n"
            "• Will be auto-cropped to 1:1 square\n"
            "• Must be a valid image file",
            parse_mode="Markdown"
        )

        return ICON_IMAGE

    async def skip_socials(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Skip social links"""
        query = update.callback_query
        await query.answer()

        # Go directly to icon upload - no skip option
        await query.edit_message_text(
            "⏭️ Skipped social links.\n\n"
            "📋 **Step 5/6: Images**\n\n"
            "📷 **Icon Image (Required)**\n\n"
            "Send your icon image (1:1 square):",
            parse_mode="Markdown"
        )

        return ICON_IMAGE

    # ==================== IMAGE UPLOADS ====================

    async def upload_icon_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Prompt for icon upload"""
        query = update.callback_query
        await query.answer()
        await query.message.reply_text(
            "📷 **Icon Image (Required)**\n\n"
            "Send your icon image.\n"
            "• Will be auto-cropped to 1:1 square",
            parse_mode="Markdown"
        )
        return ICON_IMAGE

    async def icon_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle icon image upload - REQUIRED, accepts both photos and file uploads"""
        session = self.get_session(update.effective_user.id)

        if update.message.text:
            await update.message.reply_text("Icon is required. Please send an image (photo or file).")
            return ICON_IMAGE

        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        file_path = None
        
        # Handle photo sent as photo
        if update.message.photo:
            photo = update.message.photo[-1]
            file = await photo.get_file()
            file_path = f"./user_uploads/icon_{update.effective_user.id}_{timestamp}.jpg"
            await file.download_to_drive(file_path)
        # Handle image sent as document/file
        elif update.message.document:
            doc = update.message.document
            mime = doc.mime_type or ""
            if mime.startswith("image/") or doc.file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                file = await doc.get_file()
                ext = os.path.splitext(doc.file_name)[1] if doc.file_name else '.jpg'
                file_path = f"./user_uploads/icon_{update.effective_user.id}_{timestamp}{ext}"
                await file.download_to_drive(file_path)
            else:
                await update.message.reply_text("Please send an image file (JPEG, PNG, or WebP).")
                return ICON_IMAGE
        
        if not file_path:
            await update.message.reply_text("Icon is required. Please send an image (photo or file).")
            return ICON_IMAGE

        await update.message.reply_text("🔄 Processing icon...")

        try:
            processed_path, status_msg = ImageProcessor.process_icon(os.path.abspath(file_path))
            session.icon_image_path = processed_path

            with Image.open(processed_path) as img:
                w, h = img.size

            keyboard = [
                [
                    InlineKeyboardButton("Proceed", callback_data="accept_icon"),
                    InlineKeyboardButton("🔄 Re-upload", callback_data="reupload_icon"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            with open(processed_path, "rb") as img_file:
                await update.message.reply_photo(
                    photo=img_file,
                    caption=f"📷 **Processed Icon**\n\n{status_msg}\n📐 Size: {w}x{h}px",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )

        except Exception as e:
            session.icon_image_path = os.path.abspath(file_path)
            await update.message.reply_text(f"⚠️ Processing issue: {e}\nUsing original.")
            return await self.prompt_header(update, context)

        return ICON_IMAGE

    async def accept_icon(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Accept icon and move to header"""
        query = update.callback_query
        await query.answer("Icon accepted!")
        return await self.prompt_header_callback(query, context)

    async def reupload_icon(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Re-upload icon"""
        query = update.callback_query
        await query.answer()

        session = self.get_session(query.from_user.id)
        session.icon_image_path = None

        await query.message.reply_text("🔄 Send a new icon image:")
        return ICON_IMAGE

    async def prompt_header(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Prompt for header upload - REQUIRED"""
        await update.message.reply_text(
            "Icon saved!\n\n"
            "🖼️ **Header Image (Required)**\n\n"
            "Send your header/banner image.\n"
            "• Will be resized to 600x200 (3:1)",
            parse_mode="Markdown"
        )
        return HEADER_IMAGE

    async def prompt_header_callback(self, query, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Prompt for header from callback - REQUIRED"""
        await query.message.reply_text(
            "Icon saved!\n\n"
            "🖼️ **Header Image (Required)**\n\n"
            "Send your header/banner image.\n"
            "• Will be resized to 600x200 (3:1)",
            parse_mode="Markdown"
        )
        return HEADER_IMAGE

    async def upload_header_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Prompt for header upload"""
        query = update.callback_query
        await query.answer()
        await query.message.reply_text(
            "🖼️ **Header Image (Required)**\n\n"
            "Send your header/banner image.\n"
            "• Will be resized to 600x200 (3:1)",
            parse_mode="Markdown"
        )
        return HEADER_IMAGE

    async def header_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle header image upload - REQUIRED, accepts both photos and file uploads"""
        session = self.get_session(update.effective_user.id)

        if update.message.text:
            await update.message.reply_text("Header is required. Please send an image (photo or file).")
            return HEADER_IMAGE

        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        file_path = None
        
        # Handle photo sent as photo
        if update.message.photo:
            photo = update.message.photo[-1]
            file = await photo.get_file()
            file_path = f"./user_uploads/header_{update.effective_user.id}_{timestamp}.jpg"
            await file.download_to_drive(file_path)
        # Handle image sent as document/file
        elif update.message.document:
            doc = update.message.document
            mime = doc.mime_type or ""
            if mime.startswith("image/") or doc.file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                file = await doc.get_file()
                ext = os.path.splitext(doc.file_name)[1] if doc.file_name else '.jpg'
                file_path = f"./user_uploads/header_{update.effective_user.id}_{timestamp}{ext}"
                await file.download_to_drive(file_path)
            else:
                await update.message.reply_text("Please send an image file (JPEG, PNG, or WebP).")
                return HEADER_IMAGE
        
        if not file_path:
            await update.message.reply_text("Header is required. Please send an image (photo or file).")
            return HEADER_IMAGE

        await update.message.reply_text("🔄 Processing header...")

        try:
            processed_path, status_msg = ImageProcessor.process_header(os.path.abspath(file_path))
            session.header_image_path = processed_path

            with Image.open(processed_path) as img:
                w, h = img.size

            keyboard = [
                [
                    InlineKeyboardButton("Proceed", callback_data="accept_header"),
                    InlineKeyboardButton("🔄 Re-upload", callback_data="reupload_header"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            with open(processed_path, "rb") as img_file:
                await update.message.reply_photo(
                    photo=img_file,
                    caption=f"🖼️ **Processed Header**\n\n{status_msg}\n📐 Size: {w}x{h}px",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )

        except Exception as e:
            session.header_image_path = os.path.abspath(file_path)
            await update.message.reply_text(f"⚠️ Processing issue: {e}\nUsing original.")
            return await self.show_confirmation(update, context)

        return HEADER_IMAGE

    async def accept_header(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Accept header and show confirmation"""
        query = update.callback_query
        await query.answer("Header accepted!")
        return await self.show_confirmation_callback(query, context)

    async def reupload_header(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Re-upload header"""
        query = update.callback_query
        await query.answer()

        session = self.get_session(query.from_user.id)
        session.header_image_path = None

        await query.message.reply_text("🔄 Send a new header image:")
        return HEADER_IMAGE

    # ==================== CONFIRMATION ====================

    async def show_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Show order summary"""
        session = self.get_session(update.effective_user.id)

        summary = self._build_summary(session)
        keyboard = self._build_confirm_keyboard()
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(summary, reply_markup=reply_markup, parse_mode="Markdown")
        return CONFIRM

    async def show_confirmation_callback(self, query, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Show confirmation from callback"""
        session = self.get_session(query.from_user.id)

        summary = self._build_summary(session)
        keyboard = self._build_confirm_keyboard()
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.message.reply_text(summary, reply_markup=reply_markup, parse_mode="Markdown")
        return CONFIRM

    def _build_confirm_keyboard(self) -> list:
        """Build confirmation keyboard with edit options"""
        return [
            [InlineKeyboardButton("Submit Order", callback_data="confirm_order")],
            [
                InlineKeyboardButton("Edit Chain", callback_data="edit_chain"),
                InlineKeyboardButton("Edit Token", callback_data="edit_token"),
            ],
            [
                InlineKeyboardButton("Edit Description", callback_data="edit_description"),
                InlineKeyboardButton("Edit Socials", callback_data="edit_socials"),
            ],
            [
                InlineKeyboardButton("Edit Icon", callback_data="edit_icon"),
                InlineKeyboardButton("Edit Header", callback_data="edit_header"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")],
        ]

    def _build_summary(self, session: UserSession) -> str:
        """Build order summary text"""
        summary = (
            "📋 **Review Order**\n\n"
            f"🔗 **Chain:** `{session.chain}`\n"
            f"📍 **Token:** `{session.token_address[:30]}...`\n"
            f"📝 **Description:** {session.description[:50]}...\n\n"
        )

        summary += "**Socials:**\n"
        summary += f"🌐 Website: {'✔' + session.website_url[:25] + '...' if session.website_url else 'None'}\n"
        summary += f"🐦 X: {'✔' + session.x_url[:25] + '...' if session.x_url else 'None'}\n"
        summary += f"📱 Telegram: {'✔' + session.telegram_url[:25] + '...' if session.telegram_url else 'None'}\n\n"

        summary += "**Images:**\n"
        summary += f"📷 Icon: {'Uploaded' if session.icon_image_path else 'None'}\n"
        summary += f"🖼️ Header: {'Uploaded' if session.header_image_path else 'None'}\n"

        return summary

    # ==================== EDIT HANDLERS ====================

    async def edit_chain(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Edit chain selection"""
        query = update.callback_query
        await query.answer()

        keyboard = []
        row = []
        for i, chain in enumerate(SUPPORTED_CHAINS):
            row.append(InlineKeyboardButton(chain, callback_data=f"chain_{chain}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("« Back", callback_data="back_to_confirm")])

        await query.edit_message_text(
            "✏️ **Edit Chain**\n\nSelect new blockchain:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return CHAIN

    async def edit_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Edit token address"""
        query = update.callback_query
        await query.answer()
        await query.message.reply_text("✏️ **Edit Token**\n\nEnter new token address:", parse_mode="Markdown")
        return TOKEN_ADDRESS

    async def edit_description(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Edit description"""
        query = update.callback_query
        await query.answer()
        await query.message.reply_text("✏️ **Edit Description**\n\nEnter new description:", parse_mode="Markdown")
        return DESCRIPTION

    async def edit_socials(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Edit social links - choose which one to edit"""
        query = update.callback_query
        await query.answer()

        session = self.get_session(query.from_user.id)

        # Show current values and edit options
        website_status = f"✔ {session.website_url[:20]}..." if session.website_url else "❌ Not set"
        x_status = f"✔ {session.x_url[:20]}..." if session.x_url else "❌ Not set"
        tg_status = (
            f"✔ {session.telegram_url[:20]}..."
            if session.telegram_url
            else "❌ Not set"
        )

        keyboard = [
            [InlineKeyboardButton(f"🌐 Website: {website_status}", callback_data="edit_website")],
            [InlineKeyboardButton(f"🐦 X: {x_status}", callback_data="edit_x")],
            [InlineKeyboardButton(f"📱 Telegram: {tg_status}", callback_data="edit_telegram")],
            [InlineKeyboardButton("« Back to Review", callback_data="back_to_confirm")],
        ]

        await query.edit_message_text(
            "✏️ **Edit Social Links**\n\n"
            "Select which social link to edit:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WEBSITE

    async def edit_website_only(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Edit website URL only"""
        query = update.callback_query
        await query.answer()
        context.user_data["editing_social"] = "website"
        await query.message.reply_text(
            "🌐 **Edit Website URL**\n\nEnter new website URL (or 'skip' to remove):",
            parse_mode="Markdown"
        )
        return WEBSITE

    async def edit_x_only(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Edit X URL only"""
        query = update.callback_query
        await query.answer()
        context.user_data["editing_social"] = "x"
        await query.message.reply_text(
            "🐦 **Edit X (Twitter) URL**\n\nEnter new X URL (or 'skip' to remove):",
            parse_mode="Markdown"
        )
        return WEBSITE

    async def edit_telegram_only(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Edit Telegram URL only"""
        query = update.callback_query
        await query.answer()
        context.user_data["editing_social"] = "telegram"
        await query.message.reply_text(
            "📱 **Edit Telegram URL**\n\nEnter new Telegram URL (or 'skip' to remove):",
            parse_mode="Markdown"
        )
        return WEBSITE

    async def edit_icon(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Edit icon image"""
        query = update.callback_query
        await query.answer()

        session = self.get_session(query.from_user.id)
        session.icon_image_path = None

        await query.message.reply_text(
            "✏️ **Edit Icon**\n\n📷 Send new icon image (will be cropped to 1:1):",
            parse_mode="Markdown"
        )
        return ICON_IMAGE

    async def edit_header(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Edit header image"""
        query = update.callback_query
        await query.answer()

        session = self.get_session(query.from_user.id)
        session.header_image_path = None

        await query.message.reply_text(
            "✏️ **Edit Header**\n\n🖼️ Send new header image (will be resized to 600x200):",
            parse_mode="Markdown"
        )
        return HEADER_IMAGE

    async def edit_images(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Edit images after rejection"""
        query = update.callback_query
        await query.answer()

        session = self.get_session(query.from_user.id)
        session.icon_image_path = None
        session.header_image_path = None

        await query.message.reply_text(
            "🔄 **Re-upload Images**\n\n📷 Send your icon image (1:1 square):",
            parse_mode="Markdown"
        )
        return ICON_IMAGE

    async def back_to_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Go back to confirmation"""
        query = update.callback_query
        await query.answer()
        return await self.show_confirmation_callback(query, context)

    # ==================== ORDER SUBMISSION ====================

    async def confirm_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Start the automation in background - handler returns immediately"""
        query = update.callback_query
        await query.answer()

        session = self.get_session(query.from_user.id)
        chat_id = query.from_user.id
        user_id = query.from_user.id

        # Start automation as background task - don't block the handler
        asyncio.create_task(
            self._run_automation_background(session, chat_id, user_id, context)
        )

        # Return to main menu immediately - automation runs in background
        return MAIN_MENU

    async def _run_automation_background(self, session, chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
        """Background task that runs the automation without blocking other handlers"""
        username = None
        try:
            # Get username for queue display
            try:
                user = await context.bot.get_chat(chat_id)
                username = user.first_name or f"User {user_id}"
            except:
                username = f"User {user_id}"

            # Add to active queue
            self.active_queue[user_id] = {
                "username": username,
                "started_at": datetime.now(),
                "token": session.token_address[:8] + "..." if session.token_address else "Unknown"
            }

            # Build queue status message
            queue_count = len(self.active_queue)
            queue_info = ""
            if queue_count > 1:
                other_users = [v["username"] for k, v in self.active_queue.items() if k != user_id]
                queue_info = f"\n\n👥 **Active Queue ({queue_count}):**\n" + "\n".join([f"• {u}" for u in other_users[:5]])
                if len(other_users) > 5:
                    queue_info += f"\n• ...and {len(other_users) - 5} more"

            # Send initial status message with queue info
            status_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"**Processing Your Order...**\n\n⏳ Starting automation...{queue_info}",
                parse_mode="Markdown"
            )

            status_lines = []

            async def status_callback(message: str):
                status_lines.append(message)
                display_lines = status_lines[-6:]
                # Update queue count in real-time
                current_queue = len(self.active_queue)
                queue_text = f"\n\n👥 **{current_queue} active automation(s)**" if current_queue > 1 else ""
                try:
                    await status_msg.edit_text(
                        "**Processing Order**\n\n" + "\n".join(display_lines) + queue_text,
                        parse_mode="Markdown"
                    )
                except:
                    pass

            # Run automation
            result = await self.automation.submit_order(session, status_callback, user_id)

            # Cleanup temp profile
            await self.automation.cleanup_user_profile(user_id)

            # Delete status message
            try:
                await status_msg.delete()
            except:
                pass

            # Send result messages
            await self._send_automation_result(chat_id, user_id, result, context)

        except Exception as e:
            logger.error(f"Background automation error: {e}", exc_info=True)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ **Automation Error**\n\n{str(e)[:200]}",
                    parse_mode="Markdown"
                )
            except:
                pass

        finally:
            # Remove from active queue
            if user_id in self.active_queue:
                del self.active_queue[user_id]

            # Cleanup session
            if user_id in self.user_sessions:
                del self.user_sessions[user_id]

    async def _send_automation_result(self, chat_id: int, user_id: int, result: dict, context: ContextTypes.DEFAULT_TYPE):
        """Send automation result messages"""
        # Handle token already paid
        if result.get("token_already_paid") or result.get("requires_takeover"):
            keyboard = [
                [InlineKeyboardButton("🔄 Try Different Token", callback_data="pay_dex")],
                [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")],
            ]

            error_details = ""
            if result.get("all_errors"):
                error_details = "\n\n**Error Details:**\n" + "\n".join([f"• {e[:80]}" for e in result["all_errors"][:3]])

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ **Dex Has already been paid for this token**\n\n"
                    f"{result.get('message', 'This token already has Enhanced Token Info.')}\n"
                    f"{error_details}\n\n"
                    "**Options:**\n"
                    "• File a Community Takeover (CTO) claim on DexScreener\n"
                    "Visit: https://marketplace.dexscreener.com/product/token-community-takeover/ to proceed with CTO."
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            return

        # Handle image errors with detailed feedback
        if result.get("image_errors"):
            error_list = "\n".join([f"• {e}" for e in result["image_errors"]])
            keyboard = [
                [InlineKeyboardButton("🔄 Re-upload Images", callback_data="edit_images")],
                [InlineKeyboardButton("New Order", callback_data="pay_dex")],
                [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")],
            ]
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ **Image Rejected by DexScreener**\n\n"
                    f"**Errors:**\n{error_list}\n\n"
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        # Handle form errors (non-critical)
        if result.get("form_errors") and not result["success"]:
            error_list = "\n".join([f"• {e[:80]}" for e in result["form_errors"][:5]])
            keyboard = [
                [InlineKeyboardButton("🔄 Try Again", callback_data="confirm_order")],
                [InlineKeyboardButton("✏️ Edit Order", callback_data="back_to_confirm")],
                [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")],
            ]
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ **Form Errors Detected**\n\n"
                    f"**Errors:**\n{error_list}\n\n"
                    "Please review and fix these issues."
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        if result["success"]:
            # Increment stats counter
            self.total_dexes_processed += 1
            
            # Show any warnings (non-blocking errors)
            warnings_text = ""
            if result.get("form_errors"):
                warnings_text = "\n\n⚠️ **Warnings:**\n" + "\n".join([f"• {e[:50]}" for e in result["form_errors"][:3]])

            # Build order info
            order_info = ""
            if result.get("order_number"):
                order_info = f"📋 **Order:** #{result['order_number']}\n\n"

            # Send initial success message
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "**Order Submitted Successfully!**\n\n"
                    f"{order_info}"
                    "Screenshots and payment link below..."
                    f"{warnings_text}"
                ),
                parse_mode="Markdown"
            )

            # Send payment options page screenshot FIRST
            if result.get("payment_page_screenshot") and os.path.exists(result["payment_page_screenshot"]):
                with open(result["payment_page_screenshot"], "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=chat_id, 
                        photo=photo,
                        caption="📸 Payment options page"
                    )

            # Send QR code page screenshot
            if result.get("qr_page_screenshot") and os.path.exists(result["qr_page_screenshot"]):
                with open(result["qr_page_screenshot"], "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=chat_id, 
                        photo=photo,
                        caption="📸 QR code payment page"
                    )
            elif result.get("screenshot_path") and os.path.exists(result["screenshot_path"]):
                # Fallback to single screenshot
                with open(result["screenshot_path"], "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=chat_id, 
                        photo=photo,
                        caption="📸 Payment page screenshot"
                    )

            # Send payment link AFTER screenshots
            if result["payment_url"]:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "💳 **Payment Link:**\n\n"
                        f"[Click here to pay]({result['payment_url']})"
                    ),
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Could not extract payment link automatically. Please use the QR code from the screenshots above.",
                    parse_mode="Markdown"
                )
        else:
            # General failure with error details
            error_details = ""
            if result.get("all_errors"):
                error_details = "\n\n**Error Details:**\n" + "\n".join([f"• {e[:80]}" for e in result["all_errors"][:5]])

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ **Order Failed**\n\n"
                    f"{result.get('message', 'Unknown error occurred')}"
                    f"{error_details}"
                ),
                parse_mode="Markdown"
            )

        # Final menu
        keyboard = [
            [InlineKeyboardButton("New Order", callback_data="pay_dex")],
            [InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")],
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text="What would you like to do next?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def cancel_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel order"""
        query = update.callback_query
        await query.answer()

        if query.from_user.id in self.user_sessions:
            del self.user_sessions[query.from_user.id]

        keyboard = [
            [InlineKeyboardButton("New Order", callback_data="pay_dex")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "❌ Order cancelled.",
            reply_markup=reply_markup
        )

        return MAIN_MENU

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel command"""
        user_id = update.effective_user.id
        if user_id in self.user_sessions:
            del self.user_sessions[user_id]

        await update.message.reply_text("❌ Cancelled. Use /start to begin again.")
        return ConversationHandler.END

    # ==================== OTHER COMMANDS ====================

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Help command"""
        await update.message.reply_text(
            "🤖 **DexScreener Bot**\n\n"
            "/start - Main menu\n"
            "/paydex - Pay for listing\n"
            "/resize - Resize image\n"
            "/stats - View stats\n"
            "/cancel - Cancel",
            parse_mode="Markdown"
        )

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Stats command - show total dexes processed"""
        active_count = len(self.active_queue)
        await update.message.reply_text(
            "📊 **Bot Statistics**\n\n"
            f"**Total Dexes Processed:** {self.total_dexes_processed}\n"
            f"🔄 **Currently Active:** {active_count}",
            parse_mode="Markdown"
        )

    async def resize_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Direct /resize command"""
        user = update.effective_user
        self.user_sessions[user.id] = UserSession()

        keyboard = [
            [
                InlineKeyboardButton("📷 Icon (1:1)", callback_data="resize_icon"),
                InlineKeyboardButton("🖼️ Header (3:1)", callback_data="resize_header"),
            ],
            [InlineKeyboardButton("« Back", callback_data="back_to_main")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🖼️ **Image Resize**\n\n"
            "• **Icon** - Square 1:1\n"
            "• **Header** - 600x200 (3:1)",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

        return RESIZE_TYPE

    async def paydex_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Direct /paydex command"""
        user = update.effective_user
        self.user_sessions[user.id] = UserSession()

        keyboard = []
        row = []
        for i, chain in enumerate(SUPPORTED_CHAINS):
            row.append(InlineKeyboardButton(chain, callback_data=f"chain_{chain}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("« Back", callback_data="back_to_main")])

        await update.message.reply_text(
            "**DexScreener Listing**\n\n"
            "Step 1/6: Select blockchain:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        return CHAIN

    async def login_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check session status OR import a session.json file sent as attachment."""
        import json

        # If user sent a document with /login, import it
        if update.message.document:
            await self.import_session_file(update, context)
            return

        # Otherwise, check current session status
        await update.message.reply_text("🔍 Checking saved DexScreener session...")

        session_file = "./session.json"
        session_info = ""
        if os.path.exists(session_file):
            try:
                with open(session_file) as f:
                    data = json.load(f)
                cookie_count = len(data.get("cookies", []))
                session_info = f"\n📦 session.json found ({cookie_count} cookies)"
            except Exception:
                session_info = "\n⚠️ session.json found but unreadable"

        try:
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=self.automation.master_profile_dir,
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                    ],
                    viewport={"width": 1920, "height": 1080},
                )

                # Inject cookies from session.json if available
                if os.path.exists(session_file):
                    try:
                        with open(session_file) as f:
                            storage = json.load(f)
                        cookies = storage.get("cookies", [])
                        if cookies:
                            await ctx.add_cookies(cookies)
                    except Exception:
                        pass

                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://marketplace.dexscreener.com/product/token-info/order")
                await page.wait_for_timeout(4000)

                try:
                    await page.wait_for_selector("text=Sign Out", timeout=5000)
                    await ctx.close()
                    await update.message.reply_text(
                        f"✅ *Session is valid!* DexScreener is logged in and ready.{session_info}\n\n"
                        "All users can use the bot.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    await ctx.close()
                    await update.message.reply_text(
                        f"❌ *Session not found.*{session_info}\n\n"
                        "*How to fix — run this on your local Mac:*\n"
                        "```\npython export_session.py\n```\n"
                        "Then send the generated `session.json` file here as a reply to this message.",
                        parse_mode="Markdown"
                    )

        except Exception as e:
            await update.message.reply_text(f"❌ Error checking session: {e}")

    async def import_session_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Import cookies from an uploaded session.json file sent via Telegram."""
        import json

        doc = update.message.document
        if not doc.file_name.endswith(".json"):
            await update.message.reply_text("❌ Please send a `.json` file exported by `export_session.py`.")
            return

        await update.message.reply_text("📥 Importing session...")

        try:
            tg_file = await context.bot.get_file(doc.file_id)
            raw = await tg_file.download_as_bytearray()
            storage = json.loads(raw.decode("utf-8"))

            cookies = storage.get("cookies", [])
            if not cookies:
                await update.message.reply_text("❌ No cookies found in the file. Make sure you used `export_session.py`.")
                return

            with open("./session.json", "w") as f:
                json.dump(storage, f)

            await update.message.reply_text(
                f"✅ Imported {len(cookies)} cookies!\n\n"
                "Run /login to verify the session is active."
            )

        except Exception as e:
            await update.message.reply_text(f"❌ Import failed: {e}")

    async def auto_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Auto-start the bot when user sends any message"""
        return await self.start(update, context)

    # ==================== FALLBACK HANDLERS ====================
    # These handlers work even when ConversationHandler state is lost
    # (e.g., after bot restart or disconnection)

    async def pay_dex_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fallback: Pay dex when conversation state is lost"""
        query = update.callback_query
        await query.answer()
        self.user_sessions[query.from_user.id] = UserSession()

        keyboard = []
        row = []
        for i, chain in enumerate(SUPPORTED_CHAINS):
            row.append(InlineKeyboardButton(chain, callback_data=f"chain_{chain}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("« Back", callback_data="back_to_main")])

        text = (
            "**DexScreener Token Listing**\n\n"
            "📋 **Step 1/6: Select Blockchain**\n\n"
            "Choose your token's blockchain:"
        )

        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def resize_only_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fallback: Resize only when conversation state is lost"""
        query = update.callback_query
        await query.answer()
        self.user_sessions[query.from_user.id] = UserSession()

        keyboard = [
            [
                InlineKeyboardButton("📷 Icon (1:1)", callback_data="resize_icon"),
                InlineKeyboardButton("🖼️ Header (3:1)", callback_data="resize_header"),
            ],
            [InlineKeyboardButton("« Back", callback_data="back_to_main")],
        ]

        text = (
            "🖼️ **Image Resize Tool**\n\n"
            "What type of image do you want to resize?\n\n"
            "• **Icon (1:1)** - Square format for token icon\n"
            "• **Header (3:1)** - Banner format, 600px width"
        )

        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def back_to_main_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fallback: Back to main menu when conversation state is lost"""
        query = update.callback_query
        await query.answer()
        self.user_sessions[query.from_user.id] = UserSession()

        keyboard = [
            [InlineKeyboardButton("Pay for DexScreener Listing", callback_data="pay_dex")],
            [InlineKeyboardButton("🖼️ Resize Image Only", callback_data="resize_only")],
            [InlineKeyboardButton("❓ Help", callback_data="show_help")],
        ]

        text = (
            "🤖 **DexScreener Automation Bot**\n\n"
            "What would you like to do?"
        )

        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def show_help_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fallback: Show help when conversation state is lost"""
        query = update.callback_query
        await query.answer()

        keyboard = [
            [InlineKeyboardButton("Start Order", callback_data="pay_dex")],
            [InlineKeyboardButton("« Back", callback_data="back_to_main")],
        ]

        text = (
            "❓ **Help**\n\n"
            "** Pay for DexScreener Listing:**\n"
            "Automates the entire DexScreener Enhanced Token Info order process:\n"
            "1️⃣ Select blockchain\n"
            "2️⃣ Enter token address\n"
            "3️⃣ Add description\n"
            "4️⃣ Add social links\n"
            "5️⃣ Upload images\n"
            "6️⃣ Submit & get payment link\n\n"
            "**🖼️ Resize Image Only:**\n"
            "Resize images to DexScreener requirements:\n"
            "• Icon: 1:1 square ratio\n"
            "• Header: 3:1 ratio, 600px width\n\n"
            "**Commands:**\n"
            "/start - Main menu\n"
            "/cancel - Cancel operation\n"
        )

        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def resize_icon_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fallback: Resize icon when conversation state is lost"""
        query = update.callback_query
        await query.answer()
        session = self.get_session(query.from_user.id)
        session.resize_type = "icon"
        await query.message.reply_text(
            "📷 **Icon Resize (1:1)**\n\n"
            "Send me the image you want to resize to square format.",
            parse_mode="Markdown"
        )

    async def resize_header_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fallback: Resize header when conversation state is lost"""
        query = update.callback_query
        await query.answer()
        session = self.get_session(query.from_user.id)
        session.resize_type = "header"
        await query.message.reply_text(
            "🖼️ **Header Resize (3:1)**\n\n"
            "Send me the image you want to resize to banner format (600px width).",
            parse_mode="Markdown"
        )

    async def chain_selected_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fallback: Chain selected when conversation state is lost"""
        query = update.callback_query
        await query.answer()
        chain = query.data.replace("chain_", "")
        session = self.get_session(query.from_user.id)
        session.chain = chain
        await query.edit_message_text(
            f"Chain: **{chain}**\n\n"
            "📋 **Step 2/6: Token Address**\n\n"
            "Enter your token contract address:",
            parse_mode="Markdown"
        )

    # ==================== RUN BOT ====================

    def run(self):
        """Run the bot"""
        app = Application.builder().token(self.token).build()

        # Set up bot commands menu (shows when user types /) - /login hidden from users
        async def post_init(application):
            await application.bot.set_my_commands([
                ("start", "Main menu"),
                ("paydex", "Pay for DexScreener listing"),
                ("resize", "Resize image for DexScreener"),
                ("stats", "View bot statistics"),
                ("cancel", "Cancel current operation"),
            ])

        app.post_init = post_init

        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("start", self.start),
                CommandHandler("resize", self.resize_command),
                CommandHandler("paydex", self.paydex_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.auto_start),  # Auto-start on any message
            ],
            states={
                MAIN_MENU: [
                    CallbackQueryHandler(self.pay_dex, pattern="^pay_dex$"),
                    CallbackQueryHandler(self.resize_only, pattern="^resize_only$"),
                    CallbackQueryHandler(self.show_help, pattern="^show_help$"),
                    CallbackQueryHandler(self.back_to_main, pattern="^back_to_main$"),
                    CallbackQueryHandler(self.edit_images, pattern="^edit_images$"),
                ],
                RESIZE_TYPE: [
                    CallbackQueryHandler(self.resize_icon_selected, pattern="^resize_icon$"),
                    CallbackQueryHandler(self.resize_header_selected, pattern="^resize_header$"),
                    CallbackQueryHandler(self.back_to_main, pattern="^back_to_main$"),
                ],
                RESIZE_IMAGE: [
                    MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.process_resize_image),
                    CallbackQueryHandler(self.back_to_main, pattern="^back_to_main$"),
                ],
                CHAIN: [
                    CallbackQueryHandler(self.chain_selected, pattern="^chain_"),
                    CallbackQueryHandler(self.back_to_main, pattern="^back_to_main$"),
                    CallbackQueryHandler(self.back_to_confirm, pattern="^back_to_confirm$"),
                ],
                TOKEN_ADDRESS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.token_address_received),
                ],
                DESCRIPTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.description_received),
                ],
                WEBSITE: [
                    CallbackQueryHandler(self.add_website, pattern="^add_website$"),
                    CallbackQueryHandler(self.skip_socials, pattern="^skip_socials$"),
                    CallbackQueryHandler(self.back_to_confirm, pattern="^back_to_confirm$"),
                    CallbackQueryHandler(self.edit_website_only, pattern="^edit_website$"),
                    CallbackQueryHandler(self.edit_x_only, pattern="^edit_x$"),
                    CallbackQueryHandler(self.edit_telegram_only, pattern="^edit_telegram$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.website_received),
                ],
                X_URL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.x_url_received),
                ],
                TELEGRAM_URL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.telegram_url_received),
                ],
                ICON_IMAGE: [
                    CallbackQueryHandler(self.upload_icon_prompt, pattern="^upload_icon$"),
                    CallbackQueryHandler(self.accept_icon, pattern="^accept_icon$"),
                    CallbackQueryHandler(self.reupload_icon, pattern="^reupload_icon$"),
                    MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.icon_received),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.icon_received),
                ],
                HEADER_IMAGE: [
                    CallbackQueryHandler(self.upload_header_prompt, pattern="^upload_header$"),
                    CallbackQueryHandler(self.accept_header, pattern="^accept_header$"),
                    CallbackQueryHandler(self.reupload_header, pattern="^reupload_header$"),
                    MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.header_received),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.header_received),
                ],
                CONFIRM: [
                    CallbackQueryHandler(self.confirm_order, pattern="^confirm_order$"),
                    CallbackQueryHandler(self.cancel_order, pattern="^cancel_order$"),
                    CallbackQueryHandler(self.edit_chain, pattern="^edit_chain$"),
                    CallbackQueryHandler(self.edit_token, pattern="^edit_token$"),
                    CallbackQueryHandler(self.edit_description, pattern="^edit_description$"),
                    CallbackQueryHandler(self.edit_socials, pattern="^edit_socials$"),
                    CallbackQueryHandler(self.edit_icon, pattern="^edit_icon$"),
                    CallbackQueryHandler(self.edit_header, pattern="^edit_header$"),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", self.cancel),
                CommandHandler("start", self.start),
                CommandHandler("resize", self.resize_command),
                CommandHandler("paydex", self.paydex_command),
            ],
            per_message=False,  # Handle callbacks per-user, not per-message
            per_chat=True,      # Separate conversations per chat
            conversation_timeout=3600,  # 1 hour timeout to cleanup stale sessions
        )

        app.add_handler(conv_handler)
        app.add_handler(CommandHandler("help", self.help_command))
        app.add_handler(CommandHandler("stats", self.stats_command))
        app.add_handler(CommandHandler("login", self.login_setup))  # Hidden from users, only you know
        # Accept session.json uploads (sent as document without a command)
        app.add_handler(MessageHandler(filters.Document.FileExtension("json"), self.import_session_file))

        # Global fallback handlers - these work even when ConversationHandler state is lost
        # (e.g., after bot restart, disconnection, or timeout)
        # They are registered AFTER conv_handler so conv_handler takes priority when state exists
        app.add_handler(CallbackQueryHandler(self.pay_dex_fallback, pattern="^pay_dex$"))
        app.add_handler(CallbackQueryHandler(self.resize_only_fallback, pattern="^resize_only$"))
        app.add_handler(CallbackQueryHandler(self.back_to_main_fallback, pattern="^back_to_main$"))
        app.add_handler(CallbackQueryHandler(self.show_help_fallback, pattern="^show_help$"))
        app.add_handler(CallbackQueryHandler(self.resize_icon_fallback, pattern="^resize_icon$"))
        app.add_handler(CallbackQueryHandler(self.resize_header_fallback, pattern="^resize_header$"))
        app.add_handler(CallbackQueryHandler(self.chain_selected_fallback, pattern="^chain_"))

        logger.info("Bot starting...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    BOT_TOKEN = "8407195728:AAE_z5qsV_QNjhygeIHKjsMSgS8Bw-nGvJI"

    if not BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN not set")
        print("Run: export TELEGRAM_BOT_TOKEN='your_token'")
        exit(1)

    bot = DexScreenerBot(BOT_TOKEN)
    bot.run()
