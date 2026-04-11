import type { ReactNode } from "react";
import useIntersection from "../hooks/useIntersection";

interface Props {
  children: ReactNode;
}

export default function FadeInSection({ children }: Props) {
  const { ref, visible } = useIntersection();

  return (
    <div
      ref={ref}
      className={`transition-all duration-700 transform ${
        visible
          ? "opacity-100 translate-y-0"
          : "opacity-0 translate-y-10"
      }`}
    >
      {children}
    </div>
  );
}