# Basic Python program

name = input("Enter your name: ")
age = int(input("Enter your age: "))

print("Hello,", name)
print("You are", age, "years old.")

if age >= 18:
    print("You are an adult.")
else:
    print("You are under 18.")