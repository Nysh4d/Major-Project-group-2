import { useEffect, useRef } from "react";

interface Point {
  x: number;
  y: number;
  age: number;
}

export default function NeonCursorTrail() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const pointsRef = useRef<Point[]>([]);
  const mouseRef = useRef({ x: 0, y: 0 });

  useEffect(() => {
    const canvas = canvasRef.current!;
    const ctx = canvas.getContext("2d")!;
    let animationFrameId: number;

    const resize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    };

    const handleMouseMove = (e: MouseEvent) => {
      mouseRef.current = { x: e.clientX, y: e.clientY };
    };

    window.addEventListener("resize", resize);
    window.addEventListener("mousemove", handleMouseMove);
    resize();

    const render = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      // Add current mouse position to points
      pointsRef.current.push({ 
        x: mouseRef.current.x, 
        y: mouseRef.current.y, 
        age: 1 
      });

      // Update and filter points (Short trail logic)
      pointsRef.current.forEach((p) => (p.age -= 0.05)); // Disappears quickly (20 frames)
      pointsRef.current = pointsRef.current.filter((p) => p.age > 0);

      const points = pointsRef.current;

      if (points.length > 1) {
        ctx.lineJoin = "round";
        ctx.lineCap = "round";

        // SOFT LIGHT BLUE SETTINGS
        // Use a very light, desaturated blue for a "soft" feel
        const strokeColor = "rgba(173, 216, 230,"; // LightBlue base
        
        // Gentle, wide glow instead of a tight neon blur
        ctx.shadowBlur = 12;
        ctx.shadowColor = "rgba(173, 216, 230, 0.4)";

        for (let i = 1; i < points.length; i++) {
          const p1 = points[i - 1];
          const p2 = points[i];

          // Fade the opacity and thickness based on age
          ctx.beginPath();
          ctx.strokeStyle = `${strokeColor} ${p2.age * 0.5})`;
          ctx.lineWidth = 3 * p2.age; 
          
          ctx.moveTo(p1.x, p1.y);
          ctx.lineTo(p2.x, p2.y);
          ctx.stroke();
        }
      }

      animationFrameId = requestAnimationFrame(render);
    };

    render();

    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("resize", resize);
      cancelAnimationFrame(animationFrameId);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="fixed top-0 left-0 pointer-events-none z-[9999] w-full h-full"
    />
  );
}