import * as d3 from "d3";

export function renderPipeline(
  svgElement: SVGSVGElement,
  width: number,
  height: number,
  steps: {
    id: number;
    label: string[];
  }[],
  onNodeClick: (id: number) => void
) {
  const svg = d3.select(svgElement);
  svg.selectAll("*").remove();

  const centerY = height / 2;

  const xScale = d3
    .scalePoint<string>()
    .domain(steps.map((d) => String(d.id))) 
    .range([100, width - 100]);

  // ----- Gradient Definition -----
  const defs = svg.append("defs");

  const gradient = defs
    .append("linearGradient")
    .attr("id", "nodeGradient")
    .attr("x1", "0%")
    .attr("x2", "100%")
    .attr("y1", "0%")
    .attr("y2", "100%");

  gradient.append("stop").attr("offset", "0%").attr("stop-color", "#2563eb");
  gradient.append("stop").attr("offset", "100%").attr("stop-color", "#22c55e");

  steps.slice(0, -1).forEach((step, i) => {
    const next = steps[i + 1];

    svg
        .append("line")
        .attr("x1", xScale(String(step.id))!)
        .attr("y1", centerY)
        .attr("x2", xScale(String(next.id))!)
        .attr("y2", centerY)
        .attr("stroke", "#9ca3af")
        .attr("stroke-width", 2)
        .attr("stroke-dasharray", "6 6")
        .attr("stroke-dashoffset", 20)
        .transition()
        .duration(2000)
        .ease(d3.easeLinear)
        .attr("stroke-dashoffset", 0)
        .on("end", function repeat() {
        d3.select(this)
            .attr("stroke-dashoffset", 20)
            .transition()
            .duration(2000)
            .ease(d3.easeLinear)
            .attr("stroke-dashoffset", 0)
            .on("end", repeat);
        });
    });

  // ----- Nodes (Data Join Pattern) -----
  const nodes = svg
    .selectAll<SVGGElement, typeof steps[0]>("g.node")
    .data(steps)
    .join("g")
    .attr("class", "node")
    .attr(
      "transform",
      (d) => `translate(${xScale(String(d.id))}, ${centerY})`
    )
    .style("opacity", 0);

  // Animate fade in
  nodes
    .transition()
    .duration(800)
    .delay((_, i) => i * 200)
    .style("opacity", 1);

  nodes
    .append("circle")
    .attr("r", 55)
    .attr("fill", "url(#nodeGradient)");

  nodes
    .append("text")
    .attr("text-anchor", "middle")
    .attr("dominant-baseline", "middle")
    .attr("fill", "#ffffff")
    .style("font-weight", "600")
    .style("font-size", "16px")
    .each(function (d) {
        const text = d3.select(this);

        text.selectAll("tspan")
        .data(d.label)
        .enter()
        .append("tspan")
        .attr("x", 0)
        .attr("dy", (_, i) => i === 0 ? "0" : "1.2em")
        .text((line) => line);
    });

  nodes
  .on("click", (_, d) => {
    onNodeClick(d.id);
  })
  .style("cursor", "pointer");

  // Tooltip
  nodes.append("title").text((d) => `Step: ${d.label}`);
}