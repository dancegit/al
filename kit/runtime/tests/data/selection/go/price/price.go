package price

import (
	"fmt"

	"example.org/shop/cart"
)

// Label prints the cart's total.
func Label(prices []int) string {
	return fmt.Sprintf("%d EUR", cart.Total(prices))
}
