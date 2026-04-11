import { useState } from "react";

const tabs = [
  { label: "Home", id: "home" },
  { label: "Features", id: "features" },
  { label: "Architecture", id: "architecture" },
  { label: "Use Cases", id: "use-cases" },
  { label: "Contact", id: "contact" },
];

export default function Navbar() {
  const [active, setActive] = useState("home");

  const handleClick = (id: string) => {
    setActive(id);

    const section = document.getElementById(id);
    if (section) {
      section.scrollIntoView({ behavior: "smooth" });
    }
  };

  return (
    <nav className="sticky top-0 z-50 bg-white shadow-sm">
      <div className="max-w-7xl mx-auto flex justify-between items-center px-6 py-4">
        
        <h1 className="text-xl font-bold gradient-text cursor-pointer"
            onClick={() => handleClick("home")}
        >
          NeuroSense
        </h1>

        <div className="flex gap-8">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => handleClick(tab.id)}
              className="relative font-medium"
            >
              {tab.label}

              {active === tab.id && (
                <span className="absolute left-0 -bottom-1 h-[3px] w-full bg-gradient-to-r from-blue-500 via-teal-400 to-purple-500 transition-all duration-300" />
              )}
            </button>
          ))}
        </div>
      </div>
    </nav>
  );
}