import net.bull.javamelody.*;

before = Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory();
System.gc();
after = Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory();
println "Garbage collector executed: " + (before - after) / 1024 + " freed.";
