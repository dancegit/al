package cart

// Total adds the prices in the cart.
func Total(prices []int) int {
	sum := 0
	for _, p := range prices {
		sum += p
	}
	return sum
}
