# Catalog extraction

The gateway sends catalog reads to the new catalog service and all remaining traffic to the commerce monolith. Both applications currently connect to the existing commerce database during migration.

The cutover plan for catalog writes and schema ownership has not been approved.
