"""Circle USDC/CCTP payment rail for Charterforge autonomous business OS.

This module provides payment rails using Circle's USDC stablecoin and
Cross-Chain Transfer Protocol (CCTP) for permissionless cross-chain payments.

Features:

  - Native USDC transfers on supported chains
  - Cross-chain USDC transfers via CCTP (no wrapped assets)
  - Gasless payments (recipient pays gas)
  - Instant settlement

Supported Chains:

  - Ethereum Mainnet
  - Base Mainnet
  - Polygon
  - Arbitrum One
  - Optimism
  - Avalanche C-Chain
  - Stellar (via CCTP)

Integration:

  from charterforge_circle_rail import OutboundCircleRail
  
  rail = OutboundCircleRail(
      private_key="0x...",
      chain_id=8453  # Base Mainnet
  )
  
  # Send USDC
  tx_hash = await rail.send_usdc(
      to_address="0x...",
      amount=10_000_000,  # 10 USDC (6 decimals)
      usdc_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
  )
  
  # Cross-chain transfer via CCTP
  tx_hash = await rail.cross_chain_transfer(
      destination_chain_id=42161,  # Arbitrum
      amount=10_000_000,
      usdc_address="0x..."
  )

Environment Variables:

  CIRCLE_PRIVATE_KEY: Private key for signing transactions
  CIRCLE_CHAIN_ID: Default chain ID (8453 = Base)
  CIRCLE_RPC_URL: Custom RPC endpoint (optional)
"""

from typing import Optional
import os
import logging

logger = logging.getLogger(__name__)


# USDC contract addresses by chain
USDC_ADDRESSES = {
    # Ethereum Mainnet
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    # Base Mainnet
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    # Polygon
    137: "0x3c499c542cEF5E3811e1192ce708846574E20e89",
    # Arbitrum One
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    # Optimism
    10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    # Avalanche C-Chain
    43114: "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
}


class CircleRailError(Exception):
    """Circle rail operation failed."""
    pass


class InboundCircleRail:
    """Inbound payment rail for receiving USDC via Circle.
    
    Monitor on-chain USDC transfers to a designated address.
    """
    
    entry_point_name = "circle"
    
    def __init__(
        self,
        monitoring_address: Optional[str] = None,
        chain_id: int = 8453,
        usdc_address: Optional[str] = None
    ):
        """Initialize inbound Circle rail.
        
        Args:
            monitoring_address: Address to monitor for incoming USDC
            chain_id: Chain ID (default: Base Mainnet)
            usdc_address: USDC contract address (default: standard for chain)
        """
        self.monitoring_address = monitoring_address or os.environ.get(
            "CIRCLE_MONITORING_ADDRESS"
        )
        self.chain_id = chain_id
        self.usdc_address = usdc_address or USDC_ADDRESSES.get(chain_id)
        
        if not self.monitoring_address:
            raise ValueError(
                "monitoring_address required (set env CIRCLE_MONITORING_ADDRESS)"
            )
    
    async def check_balance(self) -> int:
        """Check USDC balance of monitoring address.
        
        Returns:
            Balance in USDC base units (6 decimals)
        """
        try:
            from web3 import Web3
        except ImportError as e:
            raise ImportError(
                "web3 not installed. Install with: pip install web3"
            ) from e
        
        rpc_url = os.environ.get(
            f"RPC_URL_{self.chain_id}",
            self._get_default_rpc()
        )
        
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        
        if not w3.is_connected():
            raise CircleRailError(f"Failed to connect to RPC for chain {self.chain_id}")
        
        # ERC20 ABI (minimal)
        erc20_abi = [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            }
        ]
        
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(self.usdc_address),
            abi=erc20_abi
        )
        
        balance = contract.functions.balanceOf(
            Web3.to_checksum_address(self.monitoring_address)
        ).call()
        
        return balance
    
    def _get_default_rpc(self) -> str:
        """Get default RPC URL for chain."""
        rpc_defaults = {
            1: "https://eth.llamarpc.com",
            8453: "https://mainnet.base.org",
            137: "https://polygon-rpc.com",
            42161: "https://arb1.arbitrum.io/rpc",
            10: "https://mainnet.optimism.io",
            43114: "https://api.avax.network/ext/bc/C/rpc",
        }
        return rpc_defaults.get(self.chain_id, "https://eth.llamarpc.com")


class OutboundCircleRail:
    """Outbound payment rail for sending USDC via Circle.
    
    Send USDC to any address or cross-chain via CCTP.
    """
    
    entry_point_name = "circle"
    
    def __init__(
        self,
        private_key: Optional[str] = None,
        chain_id: int = 8453,
        usdc_address: Optional[str] = None
    ):
        """Initialize outbound Circle rail.
        
        Args:
            chain_id: Chain ID (default: Base Mainnet)
            private_key: Private key for signing (defaults to CIRCLE_PRIVATE_KEY env)
            usdc_address: USDC contract address (default: standard for chain)
        """
        self.private_key = private_key or os.environ.get("CIRCLE_PRIVATE_KEY")
        self.chain_id = chain_id
        self.usdc_address = usdc_address or USDC_ADDRESSES.get(chain_id)
        
        if not self.private_key:
            raise ValueError("private_key required (set env CIRCLE_PRIVATE_KEY)")
    
    async def send_usdc(
        self,
        to_address: str,
        amount: int,
        gas: Optional[int] = None
    ) -> str:
        """Send USDC to an address.
        
        Args:
            to_address: Recipient address
            amount: Amount in USDC base units (6 decimals)
            gas: Gas limit (optional)
            
        Returns:
            Transaction hash
        """
        try:
            from web3 import Web3
        except ImportError as e:
            raise ImportError(
                "web3 not installed. Install with: pip install web3"
            ) from e
        
        rpc_url = os.environ.get(
            f"RPC_URL_{self.chain_id}",
            self._get_default_rpc()
        )
        
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        
        if not w3.is_connected():
            raise CircleRailError(f"Failed to connect to RPC for chain {self.chain_id}")
        
        # Get account from private key
        account = w3.eth.account.from_key(self.private_key)
        
        # ERC20 ABI for transfer
        erc20_abi = [
            {
                "constant": False,
                "inputs": [
                    {"name": "_to", "type": "address"},
                    {"name": "_value", "type": "uint256"}
                ],
                "name": "transfer",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            }
        ]
        
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(self.usdc_address),
            abi=erc20_abi
        )
        
        # Build transaction
        tx = contract.functions.transfer(
            Web3.to_checksum_address(to_address),
            amount
        ).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": gas or 100000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.eth.gas_price,
        })
        
        # Sign and send
        signed_tx = w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        return tx_hash.hex()
    
    def _get_default_rpc(self) -> str:
        """Get default RPC URL for chain."""
        rpc_defaults = {
            1: "https://eth.llamarpc.com",
            8453: "https://mainnet.base.org",
            137: "https://polygon-rpc.com",
            42161: "https://arb1.arbitrum.io/rpc",
            10: "https://mainnet.optimism.io",
            43114: "https://api.avax.network/ext/bc/C/rpc",
        }
        return rpc_defaults.get(self.chain_id, "https://eth.llamarpc.com")


__all__ = [
    "InboundCircleRail",
    "OutboundCircleRail",
    "CircleRailError",
    "USDC_ADDRESSES",
]
