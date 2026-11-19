package price

import "testing"

func TestLabel(t *testing.T) {
	if Label([]int{1, 2}) != "3 EUR" {
		t.Fatal("wrong label")
	}
}
