# Checkout runtime notes

The web storefront sends `POST /checkout` over HTTPS to the Checkout API.
The Checkout API calls the Pricing Service synchronously to calculate the final total.
After validation, the API writes a pending order to Orders PostgreSQL, the authoritative order store.
After that write succeeds, the API publishes an `OrderPlaced` event to the managed Orders queue.
The Fulfillment Worker consumes `OrderPlaced` events and calls the external Shipping Provider over HTTPS.

The team has selected a payment provider, but has not decided whether charging occurs inline before the checkout response or asynchronously after `OrderPlaced`.
