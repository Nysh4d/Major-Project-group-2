import ReactMarkdown from "react-markdown";

interface SectionPanelProps {
  title: string;
  content: string;
}

export default function SectionPanel({ title, content }: SectionPanelProps) {
  return (
    <div className="bg-gray-900 rounded-xl shadow-lg overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-700">
        <h3 className="text-xl font-semibold text-blue-400">{title}</h3>
      </div>
      <div className="px-6 py-6 prose prose-invert prose-sm max-w-none
                      prose-headings:text-teal-400 prose-strong:text-white
                      prose-code:text-green-400 prose-code:bg-gray-800 prose-code:px-1 prose-code:rounded
                      prose-li:text-gray-300 prose-p:text-gray-300
                      prose-a:text-blue-400">
        <ReactMarkdown>{content}</ReactMarkdown>
      </div>
    </div>
  );
}
