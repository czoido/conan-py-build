#include <pybind11/pybind11.h>
#include <spdlog/spdlog.h>
#include <string>

namespace py = pybind11;

double add(double a, double b) {
    double result = a + b;
    spdlog::info("{} + {} = {}", a, b, result);
    return result;
}

long add_integers(long a, long b) {
    long result = a + b;
    spdlog::info("(integers) {} + {} = {}", a, b, result);
    return result;
}

std::string greet(const std::string& name) {
    std::string greeting = "Hello, " + name + "! Formatted with spdlog.";
    spdlog::info("{}", greeting);
    return greeting;
}

PYBIND11_MODULE(_core, m) {
    m.doc() = "Example Python extension using spdlog (depends on fmt) via Conan.";
    m.def("add", &add, "Add two numbers.", py::arg("a"), py::arg("b"));
    m.def("add_integers", &add_integers, "Add two integers.", py::arg("a"), py::arg("b"));
    m.def("greet", &greet, "Return a greeting.", py::arg("name"));
}
