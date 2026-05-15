from typing import Optional

import httpx


class EmailService:
    def __init__(self):
        self.api_key: Optional[str] = None
        self.from_email: Optional[str] = None
        self.base_url = "https://api.resend.com/emails"

    def configure(self, api_key: str, from_email: str):
        self.api_key = api_key
        self.from_email = from_email

    async def send_email(
        self,
        to: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None
    ) -> bool:
        if not self.api_key or not self.from_email:
            return False

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "from": self.from_email,
            "to": [to],
            "subject": subject,
            "html": html_content,
        }

        if text_content:
            payload["text"] = text_content

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.base_url,
                    headers=headers,
                    json=payload,
                    timeout=30.0
                )
                return response.status_code == 200
        except Exception:
            return False

    async def send_verification_email(self, to: str, name: str, verification_url: str) -> bool:
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: #1e3a5f; color: white; padding: 20px; text-align: center; }}
                .content {{ padding: 30px 20px; background: #f9f9f9; }}
                .button {{ display: inline-block; padding: 12px 30px; background: #1e3a5f; color: white; text-decoration: none; border-radius: 5px; margin: 20px 0; }}
                .footer {{ padding: 20px; text-align: center; font-size: 12px; color: #666; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>PaySpyre</h1>
                </div>
                <div class="content">
                    <h2>Welcome to PaySpyre, {name}!</h2>
                    <p>Thank you for creating an account. Please verify your email address to complete your registration.</p>
                    <p><a href="{verification_url}" class="button">Verify Email Address</a></p>
                    <p>Or copy and paste this link into your browser:</p>
                    <p style="word-break: break-all;">{verification_url}</p>
                    <p>This link will expire in 24 hours.</p>
                </div>
                <div class="footer">
                    <p>&copy; 2026 PaySpyre. All rights reserved.</p>
                    <p>If you didn't create an account, please ignore this email.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"""
        Welcome to PaySpyre, {name}!

        Thank you for creating an account. Please verify your email address by visiting:
        {verification_url}

        This link will expire in 24 hours.

        If you didn't create an account, please ignore this email.
        """

        return await self.send_email(
            to=to,
            subject="Verify Your PaySpyre Account",
            html_content=html_content,
            text_content=text_content
        )

    async def send_password_reset_email(self, to: str, name: str, reset_url: str) -> bool:
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: #1e3a5f; color: white; padding: 20px; text-align: center; }}
                .content {{ padding: 30px 20px; background: #f9f9f9; }}
                .button {{ display: inline-block; padding: 12px 30px; background: #1e3a5f; color: white; text-decoration: none; border-radius: 5px; margin: 20px 0; }}
                .footer {{ padding: 20px; text-align: center; font-size: 12px; color: #666; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>PaySpyre</h1>
                </div>
                <div class="content">
                    <h2>Password Reset Request</h2>
                    <p>Hello {name},</p>
                    <p>We received a request to reset your password. Click the button below to create a new password:</p>
                    <p><a href="{reset_url}" class="button">Reset Password</a></p>
                    <p>Or copy and paste this link into your browser:</p>
                    <p style="word-break: break-all;">{reset_url}</p>
                    <p>This link will expire in 1 hour.</p>
                    <p>If you didn't request this password reset, please ignore this email and your password will remain unchanged.</p>
                </div>
                <div class="footer">
                    <p>&copy; 2026 PaySpyre. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"""
        Hello {name},

        We received a request to reset your password. Visit the following link to create a new password:
        {reset_url}

        This link will expire in 1 hour.

        If you didn't request this password reset, please ignore this email and your password will remain unchanged.
        """

        return await self.send_email(
            to=to,
            subject="Reset Your PaySpyre Password",
            html_content=html_content,
            text_content=text_content
        )


email_service = EmailService()