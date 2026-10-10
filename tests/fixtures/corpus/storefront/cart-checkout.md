# Cart and checkout

Updated: 2026-03-02

How the cart and checkout flow behave for shoppers.

- SHOP-101: signed-in customers keep cart items for 30 days while guests keep them for 3 days.
- SHOP-102: checkout rejects an order when the postal code fails validation twice in a row.
- SHOP-101 and SHOP-102 both touch the checkout validation module, so review them together.
- Gift wrapping is offered per order, never per line item.
