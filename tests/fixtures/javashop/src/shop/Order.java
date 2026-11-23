package shop;

import java.util.List;

public record Order(String id, List<Integer> lots, double weight) {}
