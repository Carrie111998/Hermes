// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "./ConfirmDialog";

afterEach(cleanup);

describe("ConfirmDialog typed confirmation", () => {
  it("requires an exact phrase", () => {
    const onConfirm = vi.fn();
    const props = {
      onCancel: vi.fn(),
      onConfirm,
      title: "Restart gateway?",
      typedConfirmation: "RESTART",
    };
    render(<ConfirmDialog {...props} open />);

    const confirm = screen.getByRole("button", { name: "Confirm" });
    const input = screen.getByLabelText(/Type RESTART to confirm/i);
    expect(document.activeElement).toBe(input);
    expect((confirm as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(input, { target: { value: "restart" } });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: "RESTART" } });
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("clears the phrase after a prop-driven close", () => {
    const props = {
      onCancel: vi.fn(),
      onConfirm: vi.fn(),
      title: "Restart gateway?",
      typedConfirmation: "RESTART",
    };
    const { rerender } = render(<ConfirmDialog {...props} open />);

    fireEvent.change(screen.getByLabelText(/Type RESTART to confirm/i), {
      target: { value: "RESTART" },
    });
    rerender(<ConfirmDialog {...props} open={false} />);
    rerender(<ConfirmDialog {...props} open />);
    expect(
      (screen.getByLabelText(/Type RESTART to confirm/i) as HTMLInputElement)
        .value,
    ).toBe("");
    expect(
      (screen.getByRole("button", { name: "Confirm" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("does not refocus when the cancel callback identity changes", () => {
    const firstCancel = vi.fn();
    const secondCancel = vi.fn();
    const props = {
      onConfirm: vi.fn(),
      title: "Restart gateway?",
      typedConfirmation: "RESTART",
    };
    const { rerender } = render(
      <ConfirmDialog {...props} onCancel={firstCancel} open />,
    );
    const input = screen.getByLabelText(/Type RESTART to confirm/i);
    const focus = vi.spyOn(input, "focus");

    rerender(<ConfirmDialog {...props} onCancel={secondCancel} open />);

    expect(focus).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(input);
  });
});
