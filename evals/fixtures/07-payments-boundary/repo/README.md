# Checkout payment note

The checkout API sends authorization requests to an internal payment adapter. The adapter calls an external payment processor synchronously and stores the processor reference on the order. The processor later sends settlement and dispute webhooks back to the adapter.

The note does not define where card details are tokenized, how webhook retries are handled, or which service owns payment state transitions.
