import Header from "../../components/header/page";
import { FaCheckCircle, FaChartLine, FaUsers } from "react-icons/fa";
import Footer from "../../components/footer/page";
import ProtectedRoute from "../../components/auth/ProtectedRoute";

export default function About() {
  const members = [
    { name: "Richard Mlambuzi", position: "Trading Strategist & Platform Engineer", image: "/images/Richard.jpg" },
  ];

  return (
    <ProtectedRoute>
      <div className="flex flex-col min-h-screen">
      <Header />

      {/* Hero Section */}
      <div className="relative flex items-center justify-center h-[30vh] bg-stone-600 text-white text-2xl font-bold mt-14 overflow-hidden">
        <img
          src="https://i.ytimg.com/vi/OlWUn5M0xNI/maxresdefault.jpg"
          alt="About us background"
          className="absolute inset-0 w-full h-full object-cover"
        />
        <h1 className="relative z-10">About Us</h1>
      </div>

      {/* Intro Section */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 p-4 bg-white mt-10">
        <div className="bg-white p-4">
          <h1 className="font-bold text-2xl md:text-4xl">
            TRADE FOREX WITH <span className="text-sky-500">AUTOMATED</span> PRECISION!
          </h1>
        </div>
        <div className="bg-white p-4">
          <p>At Auto-FX, we use AI-powered automation to make Forex trading smarter and faster. Our bot analyzes real-time market trends and executes trades with precision, keeping you ahead 24/7.</p>
        </div>
        <div className="bg-white p-4">
          <p>Built with advanced algorithms, it adapts to market changes and makes data-driven decisions. Whether you aim to maximize profits, manage risk, or automate trading, our bot has you covered.</p>
        </div>
      </div>

      {/* Features Section */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 p-4 mt-10">
        <div className="flex items-center gap-4 p-4 bg-white border border-sky-400 rounded-lg">
          <div className="text-sky-500 text-3xl">{<FaCheckCircle />}</div>
          <div>
            <h3 className="font-bold">Automated Trading 24/7</h3>
            <p className="text-gray-600 text-sm">Trades automatically, ensuring you never miss an opportunity.</p>
          </div>
        </div>

        <div className="flex items-center gap-4 p-4 bg-white border border-sky-400 rounded-lg">
          <div className="text-sky-500 text-3xl">{<FaChartLine />}</div>
          <div>
            <h3 className="font-bold">Smart Market Insights</h3>
            <p className="text-gray-600 text-sm">Analyzes trends and key indicators for informed decisions.</p>
          </div>
        </div>

        <div className="flex items-center gap-4 p-4 bg-white border border-sky-400 rounded-lg">
          <div className="text-sky-500 text-3xl">{<FaUsers />}</div>
          <div>
            <h3 className="font-bold">Professional Team</h3>
            <p className="text-gray-600 text-sm">Experts continuously refine algorithms for reliability.</p>
          </div>
        </div>
      </div>

      {/* Media Section */}
      <div className="flex flex-col md:flex-row items-center justify-center p-4 mt-10">
        {/* Image Section */}
        <div className="w-full md:w-[40vw] h-64 md:h-[50vh] bg-black m-4 rounded-lg overflow-hidden">
          <img
            src="https://nielseniq.com/wp-content/uploads/sites/4/2021/12/GettyImages-887882750-1201x801-d21b45d.jpg"
            alt="Forex Trading Bot"
            className="w-full h-full object-cover rounded-lg"
          />
        </div>

        {/* Video Section */}
        <div className="w-full md:w-[40vw] h-64 md:h-[40vh] bg-stone-600 rounded-lg m-4 md:ml-[-10vh] overflow-hidden border-4 border-white">
          <video
            src="/videos/About.mp4"
            className="w-full h-full object-cover rounded-lg"
            autoPlay
            loop
            muted
            playsInline
          />
        </div>
      </div>

      {/* Team Section with Sky Blue Chip Backgrounds */}
      <div className="bg-stone-100 py-20 px-4">
        <h1 className="font-bold text-2xl text-center">
          <span className="text-sky-500 font-bold text-2xl">Team</span> Members
        </h1>
        <p className="mt-4 mb-10 text-center">Meet the experts behind our AI-powered trading bot, dedicated to innovation and success in Forex trading.</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-6 max-w-6xl mx-auto">
          {members.map((member, index) => (
            <div key={index} className="bg-white rounded-2xl overflow-hidden shadow-md hover:shadow-lg transition-shadow">
              <div className="h-64 w-full overflow-hidden">
                <img 
                  src={member.image} 
                  alt={member.name} 
                  className="w-full h-full object-cover hover:scale-105 transition-transform duration-300"
                />
              </div>
              <div className="p-4 text-center">
                <h3 className="font-bold text-lg mb-2">{member.name}</h3>
                <span className="inline-block bg-sky-100 text-sky-800 text-xs px-3 py-1 rounded-full">
                  {member.position}
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>

      <Footer />
      </div>
    </ProtectedRoute>
  );
}