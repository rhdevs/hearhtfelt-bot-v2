import os
import logging
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from config import MONGODB_URI, SERVICES

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self):
        self.client = None
        self.db = None
        self.connected = False
        
    def connect(self):
        """Initialize MongoDB connection with error handling"""
        try:
            if not MONGODB_URI:
                logger.warning("MONGODB_URI not configured, running in memory-only mode")
                return False
                
            self.client = MongoClient(
                MONGODB_URI,
                serverSelectionTimeoutMS=5000,  # 5 second timeout
                connectTimeoutMS=5000,
                socketTimeoutMS=5000
            )
            
            # Test the connection
            self.client.admin.command('ping')
            
            # Get database (will be created if doesn't exist)
            db_name = MONGODB_URI.split('/')[-1].split('?')[0] if '/' in MONGODB_URI else 'heartfelt_bot'
            self.db = self.client[db_name]
            
            # Create indexes
            self._create_indexes()
            
            self.connected = True
            logger.info("Successfully connected to MongoDB")
            return True
            
        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            self.connected = False
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting to MongoDB: {e}")
            self.connected = False
            return False
    
    def _create_indexes(self):
        """Create necessary indexes for optimal performance"""
        try:
            # Sessions collection indexes
            self.db.sessions.create_index("session_id", unique=True)
            self.db.sessions.create_index("user_id")
            self.db.sessions.create_index([("status", 1), ("created_at", -1)])
            
            # Messages collection indexes
            self.db.messages.create_index([("session_id", 1), ("timestamp", 1)])

            # Authorized member collections (one per registered service)
            for svc in SERVICES.values():
                self.db[svc.members_collection].create_index("telegram_id", unique=True)
                self.db[svc.members_collection].create_index("active")

            logger.info("Database indexes created successfully")
        except Exception as e:
            logger.error(f"Error creating indexes: {e}")
    
    def disconnect(self):
        """Close MongoDB connection"""
        if self.client:
            self.client.close()
            self.connected = False
            logger.info("Disconnected from MongoDB")
    
    def is_connected(self):
        """Check if database is connected and accessible"""
        if not self.connected or not self.client:
            return False
        
        try:
            # Quick ping to test connection
            self.client.admin.command('ping')
            return True
        except Exception:
            self.connected = False
            return False

# Global database instance
db_manager = DatabaseManager()
