import type { ReactNode } from "react";

interface CardProps {
  children: ReactNode;
  className?: string;
}

export default function Card({ children, className = "" }: CardProps) {
  return (
    <div
      className={`bg-white p-6 rounded-[20px] shadow-lg 
                  transition-all duration-300 
                  hover:-translate-y-2 hover:shadow-2xl 
                  ${className}`}
    >
      {children}
    </div>
    
  );
}