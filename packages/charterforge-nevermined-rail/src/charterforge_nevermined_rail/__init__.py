"""Nevermined payment rail for Charterforge autonomous business OS.

This module provides inbound and outbound payment rails using the Nevermined
protocol for AI agent-to-agent payments, enabling:

  - Agent-to-agent USDC payments (permissionless)
  - Credit-based metered access to agent APIs
  - Payment validation middleware for FastAPI
  - Autonomous agent wallet management

Integration:

  # Inbound: Receive payments for services
  from charterforge_nevermined_rail import InboundNeverminedRail
  
  rail = InboundNeverminedRail(
      nvm_api_key="live:your-api-key",
      agent_id="did:nv:...",
      plan_id="0x..."
  )
  
  # Validate incoming request has valid payment
  @app.post("/api/query")
  async def query(request: Request):
      token = request.headers.get("payment-signature")
      validation = await rail.validate_payment(token)
      if not validation.is_valid:
          raise HTTPException(402, "Payment required")
      # ... serve request ...

  # Outbound: Pay for external agent services
  from charterforge_nevermined_rail import OutboundNeverminedRail
  
  outbound = OutboundNeverminedRail(
      nvm_api_key="sandbox:your-api-key",
      subscriber_address="0x..."
  )
  
  # Order a plan and get access token
  order = await outbound.order_plan(plan_id="0x...", amount=10_000_000)
  access_token = order["accessToken"]
  
  # Use token to call paid agent API
  response = await outbound.call_agent(
      endpoint="https://api.example.com/query",
      access_token=access_token,
      payload={"prompt": "Hello"}
  )

Environment variables:

  NVM_API_KEY: Nevermined API key (live: or sandbox: prefix)
  NVM_AGENT_ID: Registered agent ID (did:nv:...)
  NVM_PLAN_ID: Payment plan ID (0x...)

References:

  - Nevermined Docs: https://nevermined.ai/docs
  - Python SDK: https://pypi.org/project/payments-py/
"""

from typing import Any, Dict, Optional
import os
import logging

# Note: payments-py is optional dependency, import at runtime
# from payments_py import Payments, PaymentOptions

logger = logging.getLogger(__name__)


class PaymentValidationError(Exception):
    """Payment validation failed."""
    pass


class InboundNeverminedRail:
    """Inbound payment rail for receiving USDC payments via Nevermined.
    
    Use this when Charterforge is providing services and needs to validate
    that incoming requests have valid payment attached.
    """
    
    entry_point_name = "nevermined"
    
    def __init__(
        self,
        nvm_api_key: Optional[str] = None,
        agent_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        environment: str = "sandbox"
    ):
        """Initialize inbound Nevermined rail.
        
        Args:
            nvm_api_key: Nevermined API key (defaults to NVM_API_KEY env)
            agent_id: Registered agent ID (defaults to NVM_AGENT_ID env)
            plan_id: Payment plan ID (defaults to NVM_PLAN_ID env)
            environment: 'sandbox' or 'live'
        """
        self.nvm_api_key = nvm_api_key or os.environ.get("NVM_API_KEY")
        self.agent_id = agent_id or os.environ.get("NVM_AGENT_ID")
        self.plan_id = plan_id or os.environ.get("NVM_PLAN_ID")
        self.environment = environment
        
        if not self.nvm_api_key:
            raise ValueError("NVM_API_KEY required (set env or pass nvm_api_key)")
        
        if not self.agent_id or not self.plan_id:
            logger.warning(
                "agent_id and plan_id not set — rail will operate in "
                "validation-only mode without plan metadata"
            )
        
        self._payments = None
    
    def _get_payments(self):
        """Lazy-load payments-py SDK."""
        if self._payments is None:
            try:
                from payments_py import Payments, PaymentOptions
            except ImportError as e:
                raise ImportError(
                    "payments-py not installed. "
                    "Install with: pip install payments-py"
                ) from e
            
            self._payments = Payments.get_instance(
                PaymentOptions(
                    nvm_api_key=self.nvm_api_key,
                    environment=self.environment
                )
            )
        return self._payments
    
    async def validate_payment(self, access_token: str) -> Dict[str, Any]:
        """Validate that access token has valid payment attached.
        
        Args:
            access_token: JWT or token from payment-signature header
            
        Returns:
            Validation result with keys:
              - is_valid: bool
              - credits_remaining: int (if valid)
              - agent_id: str (if valid)
              - plan_id: str (if valid)
              - error: str (if invalid)
        """
        if not access_token:
            return {
                "is_valid": False,
                "error": "No access token provided"
            }
        
        try:
            payments = self._get_payments()
            
            # Validate the access token
            result = payments.agents.validate_request(
                access_token=access_token,
                agent_id=self.agent_id
            )
            
            return {
                "is_valid": True,
                "credits_remaining": result.get("creditsRemaining", 0),
                "agent_id": self.agent_id,
                "plan_id": self.plan_id,
                "subscriber_address": result.get("subscriberAddress")
            }
            
        except Exception as e:
            logger.error(f"Payment validation failed: {e}")
            return {
                "is_valid": False,
                "error": str(e)
            }
    
    async def consume_credit(
        self,
        access_token: str,
        credits: int = 1
    ) -> Dict[str, Any]:
        """Consume credits from the payment.
        
        Args:
            access_token: Valid access token
            credits: Number of credits to consume
            
        Returns:
            Result with remaining credits
        """
        try:
            payments = self._get_payments()
            
            result = payments.agents.consume_credits(
                access_token=access_token,
                credits=credits
            )
            
            return {
                "success": True,
                "credits_remaining": result.get("creditsRemaining", 0)
            }
            
        except Exception as e:
            logger.error(f"Credit consumption failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }


class OutboundNeverminedRail:
    """Outbound payment rail for paying other agents via Nevermined.
    
    Use this when Charterforge needs to pay for external agent services.
    """
    
    entry_point_name = "nevermined"
    
    def __init__(
        self,
        nvm_api_key: Optional[str] = None,
        subscriber_address: Optional[str] = None,
        environment: str = "sandbox"
    ):
        """Initialize outbound Nevermined rail.
        
        Args:
            nvm_api_key: Nevermined API key (defaults to NVM_API_KEY env)
            subscriber_address: Wallet address for payments
            environment: 'sandbox' or 'live'
        """
        self.nvm_api_key = nvm_api_key or os.environ.get("NVM_API_KEY")
        self.subscriber_address = subscriber_address or os.environ.get(
            "NVM_SUBSCRIBER_ADDRESS"
        )
        self.environment = environment
        
        if not self.nvm_api_key:
            raise ValueError("NVM_API_KEY required")
        
        self._payments = None
    
    def _get_payments(self):
        """Lazy-load payments-py SDK."""
        if self._payments is None:
            try:
                from payments_py import Payments, PaymentOptions
            except ImportError as e:
                raise ImportError(
                    "payments-py not installed. "
                    "Install with: pip install payments-py"
                ) from e
            
            self._payments = Payments.get_instance(
                PaymentOptions(
                    nvm_api_key=self.nvm_api_key,
                    environment=self.environment
                )
            )
        return self._payments
    
    async def order_plan(
        self,
        plan_id: str,
        amount: int,
        token_address: Optional[str] = None
    ) -> Dict[str, Any]:
        """Order a payment plan and get access token.
        
        Args:
            plan_id: Nevermined plan ID
            amount: Amount to pay (in token base units, e.g. 10_000_000 for 10 USDC)
            token_address: ERC20 token address (defaults to USDC on Base)
            
        Returns:
            Order result with accessToken
        """
        payments = self._get_payments()
        
        # Default: USDC on Base Sepolia (sandbox) or Base Mainnet (live)
        if token_address is None:
            if self.environment == "sandbox":
                token_address = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
            else:
                # Base Mainnet USDC
                token_address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        
        try:
            result = payments.plans.order_plan(
                plan_id=plan_id,
                amount=amount,
                token_address=token_address,
                subscriber_address=self.subscriber_address
            )
            
            return {
                "success": True,
                "access_token": result.get("accessToken"),
                "plan_id": plan_id,
                "amount_paid": amount
            }
            
        except Exception as e:
            logger.error(f"Plan ordering failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def call_agent(
        self,
        endpoint: str,
        access_token: str,
        payload: Dict[str, Any],
        method: str = "POST"
    ) -> Dict[str, Any]:
        """Call an agent API with payment token.
        
        Args:
            endpoint: Agent API endpoint URL
            access_token: Access token from order_plan
            payload: Request payload
            method: HTTP method
            
        Returns:
            Agent response
        """
        import httpx
        
        headers = {
            "Content-Type": "application/json",
            "payment-signature": access_token
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=method,
                    url=endpoint,
                    headers=headers,
                    json=payload,
                    timeout=60.0
                )
                
                if response.status_code == 402:
                    return {
                        "success": False,
                        "error": "Payment required",
                        "status_code": 402
                    }
                
                response.raise_for_status()
                
                return {
                    "success": True,
                    "data": response.json(),
                    "status_code": response.status_code
                }
                
        except httpx.HTTPStatusError as e:
            logger.error(f"Agent call failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "status_code": e.response.status_code
            }
        except Exception as e:
            logger.error(f"Agent call failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }


__all__ = [
    "InboundNeverminedRail",
    "OutboundNeverminedRail",
    "PaymentValidationError",
]
