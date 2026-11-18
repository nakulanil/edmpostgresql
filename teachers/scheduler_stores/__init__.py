"""
Custom DjangoJobStore that doesn't close database connections during shutdown.
This allows tests to continue using the same database connection after scheduler operations.
"""

from django_apscheduler.jobstores import DjangoJobStore


class TestJobStore(DjangoJobStore):
    """
    Custom DjangoJobStore that overrides shutdown() to not close database connections.
    
    This allows tests to continue using the same database connection after
    scheduler operations that use --once mode.
    """
    
    def shutdown(self):
        """
        Override shutdown to not close database connections.
        
        In test environments, we need to preserve the database connection
        for subsequent test operations after scheduler shutdown.
        """
        # Don't call the parent shutdown method to avoid closing the database connection
        # The parent shutdown method calls db.connection.close()
        # Instead, we just call the parent's _shutdown method which doesn't close connections
        try:
            self._shutdown()
        except Exception as e:
            # Log the error but don't fail
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Error in TestJobStore.shutdown: {e}")
        
        # Note: We don't call db.connection.close() here to preserve the connection
        # for test operations that follow