/**
 * The shared filter control.
 *
 * Two properties, both about long lists:
 *
 *   * a filter may name SEVERAL values. One Store answers "how is Nehru Place
 *     doing"; it cannot answer "how are these six shops doing", which is the
 *     question people actually bring.
 *   * a filter can be searched. Forty Stores is a scroll; two hundred is a
 *     hunt. The list is exactly as long as the estate, so a control that can
 *     only be scrolled gets slower to use the more there is to use it on.
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { FilterSelect } from "./AdminFilters";

function manyStores(count) {
  return Array.from({ length: count }, (_, index) => ({
    value: String(index + 1),
    label: `Store ${String(index + 1).padStart(3, "0")}`,
  }));
}

function Harness({ options, initial = "", multiple = true }) {
  const [value, setValue] = React.useState(initial);
  return (
    <>
      <FilterSelect label="Store" testId="filter" allLabel="All Stores"
                    value={value} onChange={setValue} options={options}
                    multiple={multiple} />
      <output data-testid="value">{value}</output>
    </>
  );
}

test("a short list has no search box, because it does not need one", () => {
  render(<Harness options={manyStores(4)} />);
  fireEvent.click(screen.getByTestId("filter"));
  expect(screen.queryByTestId("filter-search")).toBeNull();
});

test("a long list can be searched", () => {
  render(<Harness options={manyStores(40)} />);
  fireEvent.click(screen.getByTestId("filter"));

  fireEvent.change(screen.getByTestId("filter-search"),
                   { target: { value: "017" } });
  expect(screen.getByTestId("filter-option-17")).toBeTruthy();
  expect(screen.queryByTestId("filter-option-18")).toBeNull();
});

test("a search that matches nothing says so rather than looking empty", () => {
  render(<Harness options={manyStores(40)} />);
  fireEvent.click(screen.getByTestId("filter"));
  fireEvent.change(screen.getByTestId("filter-search"),
                   { target: { value: "zzz" } });

  expect(screen.getByTestId("filter-no-match").textContent).toContain("zzz");
});

test("what is already chosen stays visible while searching", () => {
  // Filtering a chosen value out of sight would let somebody untick something
  // they can no longer see - or, worse, leave it in effect invisibly.
  render(<Harness options={manyStores(40)} initial="3" />);
  fireEvent.click(screen.getByTestId("filter"));
  fireEvent.change(screen.getByTestId("filter-search"),
                   { target: { value: "017" } });

  expect(screen.getByTestId("filter-option-3")).toBeTruthy();
  expect(screen.getByTestId("filter-option-17")).toBeTruthy();
});

test("the search term is forgotten when the panel closes", () => {
  // A stale term inside a closed panel is a filter that looks empty when it
  // is not.
  render(<Harness options={manyStores(40)} />);
  fireEvent.click(screen.getByTestId("filter"));
  fireEvent.change(screen.getByTestId("filter-search"),
                   { target: { value: "017" } });
  fireEvent.click(screen.getByTestId("filter"));
  fireEvent.click(screen.getByTestId("filter"));

  expect(screen.getByTestId("filter-search").value).toBe("");
  expect(screen.getByTestId("filter-option-18")).toBeTruthy();
});

test("choosing several values sends them comma-separated", () => {
  render(<Harness options={manyStores(10)} />);
  fireEvent.click(screen.getByTestId("filter"));
  fireEvent.click(screen.getByTestId("filter-option-2"));
  fireEvent.click(screen.getByTestId("filter-option-5"));

  expect(screen.getByTestId("value").textContent).toBe("2,5");
  expect(screen.getByTestId("filter").textContent).toContain("2 selected");
});

test("clicking a chosen value removes it rather than replacing the lot", () => {
  render(<Harness options={manyStores(10)} initial="2,5" />);
  fireEvent.click(screen.getByTestId("filter"));
  fireEvent.click(screen.getByTestId("filter-option-2"));

  expect(screen.getByTestId("value").textContent).toBe("5");
});

test("the All option clears everything", () => {
  render(<Harness options={manyStores(10)} initial="2,5" />);
  fireEvent.click(screen.getByTestId("filter"));
  fireEvent.click(screen.getByTestId("filter-clear"));

  expect(screen.getByTestId("value").textContent).toBe("");
  expect(screen.getByTestId("filter").textContent).toContain("All Stores");
});

test("a single-value filter is still a plain dropdown", () => {
  // Some filters are genuinely exclusive - a Store is in ONE lifecycle state,
  // and "archived and active" is not a question anybody has.
  render(<Harness options={manyStores(3)} multiple={false} />);
  const control = screen.getByTestId("filter");
  expect(control.tagName).toBe("SELECT");
  fireEvent.change(control, { target: { value: "2" } });
  expect(screen.getByTestId("value").textContent).toBe("2");
});
